"""The bridge loop: read Home Assistant's Bluetooth scanners, post a batch, keep the engine's answers."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .client import CannotConnect, EngineClient, InvalidAuth
from .const import (
    BACKOFF_MAX,
    BACKOFF_MIN,
    CENSUS_MAX,
    CENSUS_WINDOW,
    CONF_CENSUS_INTERVAL,
    CONF_POLL_INTERVAL,
    DEDUPE_KEEP,
    DEFAULT_CENSUS_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DETAIL_CENSUS_INTERVAL,
    DOMAIN,
    PROTOCOL_VERSION,
    SIGNAL_REMOVED,
    SIGNAL_UPDATE,
    STALE_AFTER,
)
from .reader import CensusDetail, SightingReader, census

try:  # the clock habluetooth stamps sightings with
    from bluetooth_data_tools import monotonic_time_coarse
except ImportError:  # pragma: no cover - Bluetooth integration not installed
    from time import monotonic as monotonic_time_coarse

LOGGER = logging.getLogger(__package__)


def current_scanners(hass: HomeAssistant) -> list[Any]:
    """Home Assistant's active Bluetooth scanners (none if the Bluetooth integration isn't loaded)."""
    if "bluetooth" not in hass.config.components:
        return []
    from homeassistant.components import bluetooth

    return list(bluetooth.async_current_scanners(hass))


class BridgeRunner:
    """One per config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: EngineClient) -> None:
        self.hass, self.entry, self.client = hass, entry, client
        self.reader = SightingReader()
        self.poll = float(entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
        self.census_every = float(entry.options.get(CONF_CENSUS_INTERVAL, DEFAULT_CENSUS_INTERVAL))
        self.wanted: set[str] = set()  # nothing is sent until the engine says what it wants
        self.tracked: dict[str, dict[str, Any]] = {}
        self.last_ok: float | None = None
        self.signal = SIGNAL_UPDATE.format(entry.entry_id)
        self.removed_signal = SIGNAL_REMOVED.format(entry.entry_id)
        self.removed: set[str] = set()
        self.detail = None  # onboarding's detailed census (Task 5); stays None with a 0.1.0-style engine
        self._synced: tuple | None = None
        self._busy = False
        self._next_try = 0.0
        self._backoff = 0.0
        self._next_census = 0.0
        self._last_warning = float("-inf")
        self._unsub: CALLBACK_TYPE | None = None

    @property
    def available(self) -> bool:
        return self.last_ok is not None and time.monotonic() - self.last_ok < STALE_AFTER

    def start(self) -> None:
        self._unsub = async_track_time_interval(
            self.hass, self._on_timer, timedelta(seconds=self.poll), cancel_on_shutdown=True
        )

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _on_timer(self, _now: Any) -> None:
        await self.tick()

    async def tick(self) -> None:
        """One poll: skipped while a post is in flight or during back-off."""
        now = time.monotonic()
        if self._busy or now < self._next_try:
            return
        self._busy = True
        try:
            await self._cycle(now)
        finally:
            self._busy = False

    async def _cycle(self, now: float) -> None:
        scanners = current_scanners(self.hass)
        sightings, health = self.reader.read(scanners, self.wanted, self.detail)
        mono = monotonic_time_coarse()
        batch: dict[str, Any] = {"v": PROTOCOL_VERSION, "sent_wall": time.time(), "sent_mono": mono,
                                 "adverts": sightings, "scanners": health}
        if now >= self._next_census:
            rows = census(scanners, mono, CENSUS_WINDOW, CENSUS_MAX)
            if self.detail is not None:
                wall = batch["sent_wall"]
                self.detail.annotate(rows, mono, self.reader.first_seen, lambda stamp: wall - (mono - stamp))
            batch["census"] = rows
            self.reader.prune(mono, DEDUPE_KEEP)
            self._next_census = now + (DETAIL_CENSUS_INTERVAL if self.detail is not None else self.census_every)
        try:
            reply = await self.client.ingest(batch)
        except (CannotConnect, InvalidAuth) as err:
            self._backoff = min(BACKOFF_MAX, max(BACKOFF_MIN, self._backoff * 2))
            self._next_try = now + self._backoff
            if now - self._last_warning > 60:
                self._last_warning = now
                LOGGER.warning("RTLS@Home engine unavailable (%s); retrying in %.1f s",
                               "bad token" if isinstance(err, InvalidAuth) else err, self._backoff)
            if not self.available:
                async_dispatcher_send(self.hass, self.signal)
            return
        if self._backoff:
            LOGGER.info("RTLS@Home engine reachable again")
        self._backoff = 0.0
        self.last_ok = now
        self.wanted = set(reply.get("wanted") or [])
        self.tracked = {r["key"]: r for r in reply.get("tracked") or [] if r.get("key")}
        self.removed = set(reply.get("removed") or [])
        if reply.get("census_detail"):
            if self.detail is None:  # onboarding just opened: collect, and send the next census within 5 s
                self.detail = CensusDetail()
                self._next_census = min(self._next_census, now + DETAIL_CENSUS_INTERVAL)
        else:
            self.detail = None
        self._sync_devices()
        async_dispatcher_send(self.hass, self.signal)

    def _sync_devices(self) -> None:
        """Delete HA devices the engine removed on purpose; follow the engine's names. A name set in HA
        (name_by_user) always wins, because HA shows it instead of `name`."""
        state = (frozenset(self.removed), tuple(sorted((k, r.get("name")) for k, r in self.tracked.items())))
        if state == self._synced:
            return
        self._synced = state
        reg = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(reg, self.entry.entry_id):
            key = next((i[1] for i in device.identifiers if i[0] == DOMAIN), None)
            if key in self.removed and key not in self.tracked:
                reg.async_remove_device(device.id)
                async_dispatcher_send(self.hass, self.removed_signal, key)
            elif key in self.tracked:
                name = self.tracked[key].get("name")
                if name and device.name != name:
                    reg.async_update_device(device.id, name=name)
