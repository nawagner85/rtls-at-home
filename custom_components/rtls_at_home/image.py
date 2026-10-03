"""The house's pictures (spec 2026-10-02 house renders), drawn by the engine: per tracked device its *Map*, and on the
engine device a map per floor and the *House map*. Each is marked new only when what it shows changes (decision 10),
so dashboards refetch it only then, and keeps its last picture."""

from __future__ import annotations

import logging
from asyncio import Lock, sleep
from datetime import datetime
from time import monotonic
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import RtlsConfigEntry
from .client import CannotConnect, InvalidAuth, NotReady
from .const import RENDER_WAIT
from .entity import ChangeWriter, device_info, engine_device
from .pictures import Clock, snapshot
from .runner import BridgeRunner

LOGGER = logging.getLogger(__package__)
RENDER = "/api/ingest/render"


def render_meta(runner: BridgeRunner) -> dict[str, Any] | None:
    """The meta's `render` (protocol v1): the applied house's hash and its floors; None from an older engine."""
    render = (runner.meta or {}).get("render")
    return render if isinstance(render, dict) else None


def shown_present(runner: BridgeRunner) -> dict[str, dict[str, Any]]:
    """The devices the floor and house pictures draw: present, and not kept off the house maps (decision 8)."""
    return {k: r for k, r in runner.tracked.items() if k not in runner.hidden and r.get("status") != "away"}


def show_param(runner: BridgeRunner) -> dict[str, str]:
    """`show` for a floor or house picture: the tracked keys not kept off the house maps (the engine skips the away
    and the elsewhere)."""
    return {"show": ",".join(sorted(k for k in runner.tracked if k not in runner.hidden))}


async def async_setup_entry(
    hass: HomeAssistant, entry: RtlsConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runner = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def _add() -> None:
        """The house map, a map per floor and per tracked device, once the meta has `render` (an older engine never
        sends it). A floor gone from the meta keeps its entity, unavailable."""
        render = render_meta(runner)
        if render is None:
            return
        new: list[ImageEntity] = []
        if ("house", "") not in known:
            known.add(("house", ""))
            new.append(RtlsHouseMap(hass, runner))
        for floor in render.get("floors") or []:
            if floor.get("id") and ("floor", floor["id"]) not in known:
                known.add(("floor", floor["id"]))
                new.append(RtlsFloorMap(hass, runner, floor["id"], floor.get("name") or floor["id"]))
        for key in runner.tracked:
            if ("device", key) not in known:
                known.add(("device", key))
                new.append(RtlsDeviceMap(hass, runner, key))
        if new:
            async_add_entities(new)

    for signal in (runner.meta_signal, runner.signal):
        entry.async_on_unload(async_dispatcher_connect(hass, signal, _add))

    @callback
    def _forget(key: str) -> None:
        known.discard(("device", key))

    entry.async_on_unload(async_dispatcher_connect(hass, runner.removed_signal, _forget))
    _add()


class RtlsMap(ChangeWriter, ImageEntity):
    """A picture. Its clock marks it new (`image_last_updated`) on a reply or a meta that changes what it shows; it is
    fetched from the engine when a card first asks after a mark, once however many ask, and kept: the last good
    picture is served when a fetch fails (an outage, or the engine's warm-up outlasting RENDER_WAIT)."""

    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, runner: BridgeRunner, suffix: str) -> None:
        ImageEntity.__init__(self, hass)
        self._init_change(runner, suffix)
        self._clock = Clock()
        self._png: bytes | None = None
        self._png_for: datetime | None = None
        self._fetching = Lock()

    def _rows(self) -> dict[str, dict[str, Any]]:
        """The tracked rows (by key) the picture draws."""
        raise NotImplementedError

    def _path(self) -> str:
        raise NotImplementedError

    def _params(self) -> dict[str, str] | None:
        return None

    @property
    def available(self) -> bool:
        return self._runner.available and render_meta(self._runner) is not None

    @callback
    def _mark(self) -> None:
        """A new picture when the clock says what it shows changed; nothing while unavailable, so the last picture
        stands through an outage until something changes."""
        if not self.available:
            return
        snap, now = snapshot(self._rows(), (render_meta(self._runner) or {}).get("house")), monotonic()
        if self._clock.due(snap, now):
            self._clock.taken(snap, now)
            self._attr_image_last_updated = dt_util.utcnow()

    async def async_added_to_hass(self) -> None:
        self._mark()                                     # the first picture: written with the entity's first state
        await super().async_added_to_hass()

    @callback
    def _maybe_write(self) -> None:
        self._mark()
        super()._maybe_write()

    async def async_image(self) -> bytes | None:
        """The picture of the latest mark. An engine still drawing its base layers (503 after a start - an Apply
        marks every picture at once) is asked again after its Retry-After, for this same mark and under the same
        lock, until RENDER_WAIT seconds after this ask arrived - the wait for the lock included, so an ask's waiting
        ends inside Home Assistant's IMAGE_TIMEOUT (which cancels it) with time left for a last fetch. An ask whose
        budget went on another's retries serves the last good picture without asking the engine."""
        deadline = monotonic() + RENDER_WAIT
        async with self._fetching:
            if self._png is not None and self._png_for == self.image_last_updated:
                return self._png
            if monotonic() >= deadline:
                return self._png
            while True:
                stamp = self.image_last_updated
                try:
                    png = await self._runner.client.render(self._path(), self._params())
                except NotReady as err:
                    if monotonic() + err.retry_after <= deadline:
                        await sleep(err.retry_after)
                        continue
                    why = str(err)
                except (CannotConnect, InvalidAuth) as err:
                    why = str(err) or type(err).__name__
                else:
                    self._png, self._png_for = png, stamp
                    return png
                LOGGER.debug("%s: no new picture from the engine (%s)", self.entity_id, why)
                return self._png


class RtlsDeviceMap(RtlsMap):
    """The device's floor, its room tinted, its pin and 68% radius (decision 2); shown whatever its switch."""

    _attr_translation_key = "map"

    def __init__(self, hass: HomeAssistant, runner: BridgeRunner, key: str) -> None:
        super().__init__(hass, runner, f"{key}-map")
        self._key = key
        self._attr_device_info = device_info(runner, key)

    @property
    def available(self) -> bool:
        return super().available and self._key in self._runner.tracked

    def _rows(self) -> dict[str, dict[str, Any]]:
        row = self._runner.tracked.get(self._key)
        return {self._key: row} if row else {}

    def _path(self) -> str:
        return f"{RENDER}/device/{self._key}.png"


class RtlsFloorMap(RtlsMap):
    """One floor with its room names and every shown, present device on it (decision 2)."""

    _attr_translation_key = "floor_map"

    def __init__(self, hass: HomeAssistant, runner: BridgeRunner, floor_id: str, name: str) -> None:
        super().__init__(hass, runner, f"floor-{floor_id}-map")
        self._floor = floor_id
        self._attr_translation_placeholders = {"floor": name}
        self._attr_device_info = engine_device(runner)

    @property
    def available(self) -> bool:
        return super().available and any(f.get("id") == self._floor
                                         for f in (render_meta(self._runner) or {}).get("floors") or [])

    def _rows(self) -> dict[str, dict[str, Any]]:
        return {k: r for k, r in shown_present(self._runner).items() if r.get("floor_id") == self._floor}

    def _path(self) -> str:
        return f"{RENDER}/floor/{self._floor}.png"

    def _params(self) -> dict[str, str]:
        return show_param(self._runner)


class RtlsHouseMap(RtlsMap):
    """Every floor, exploded as in the viewer, with every shown, present device's pin (decision 2)."""

    _attr_translation_key = "house_map"

    def __init__(self, hass: HomeAssistant, runner: BridgeRunner) -> None:
        super().__init__(hass, runner, "house-map")
        self._attr_device_info = engine_device(runner)

    def _rows(self) -> dict[str, dict[str, Any]]:
        return shown_present(self._runner)

    def _path(self) -> str:
        return f"{RENDER}/house.png"

    def _params(self) -> dict[str, str]:
        return show_param(self._runner)
