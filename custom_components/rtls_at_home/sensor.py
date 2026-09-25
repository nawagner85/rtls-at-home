"""Room and floor sensors for every device the engine tracks."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import RtlsConfigEntry
from .const import WRITE_EVERY
from .entity import device_info
from .runner import BridgeRunner

AWAY = "Away"


def away_sentence(last_seen: float | None, last_room: str | None, now: datetime) -> str:
    """"Not detected since 6:12 AM, last in the Living Room": honest, because a quiet tag may not have left."""
    if last_seen is None:
        return "Not detected"
    seen = datetime.fromtimestamp(last_seen, now.tzinfo)
    clock = f"{seen.hour % 12 or 12}:{seen.minute:02d} {'AM' if seen.hour < 12 else 'PM'}"
    when = clock if seen.date() == now.date() else f"{seen:%b} {seen.day}, {clock}"
    text = f"Not detected since {when}"
    return f"{text}, last in the {last_room}" if last_room else text


async def async_setup_entry(
    hass: HomeAssistant, entry: RtlsConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runner = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = [key for key in runner.tracked if key not in known]
        if new:
            known.update(new)
            async_add_entities(RtlsSensor(runner, key, kind) for key in new for kind in ("room", "floor", "location"))

    entry.async_on_unload(async_dispatcher_connect(hass, runner.signal, _add_new))

    @callback
    def _forget(key: str) -> None:
        known.discard(key)

    entry.async_on_unload(async_dispatcher_connect(hass, runner.removed_signal, _forget))
    _add_new()


class RtlsSensor(SensorEntity):
    """`room` (with position attributes), `floor`, or `location` (a sentence for voice assistants) for one
    tracked device."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({"x", "y", "z", "confidence", "radius_m"})

    def __init__(self, runner: BridgeRunner, key: str, kind: str) -> None:
        self._runner, self._key, self._kind = runner, key, kind
        self._attr_unique_id = f"{runner.entry.entry_id}-{key}-{kind}"
        self._attr_translation_key = kind
        self._attr_device_info = device_info(runner, key)
        self._written: tuple[Any, bool] | None = None
        self._written_at = float("-inf")

    async def async_added_to_hass(self) -> None:
        # Home Assistant writes the first state itself when the entity is added: that is the throttle's baseline.
        self._written, self._written_at = (self.native_value, self.available), time.monotonic()
        self.async_on_remove(async_dispatcher_connect(self.hass, self._runner.signal, self._on_update))

    def _row(self) -> dict[str, Any]:
        return self._runner.tracked.get(self._key) or {}

    @callback
    def _on_update(self) -> None:
        """Write state when the value or availability changes, otherwise at most every WRITE_EVERY seconds."""
        now = time.monotonic()
        current = (self.native_value, self.available)
        if current != self._written or now - self._written_at >= WRITE_EVERY:
            self._written, self._written_at = current, now
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._runner.available and self._key in self._runner.tracked

    @property
    def native_value(self) -> str | None:
        row = self._row()
        if row.get("status") == "away":
            if self._kind == "location":
                return away_sentence(row.get("last_seen"), row.get("last_room"), dt_util.now())
            return AWAY
        if self._kind == "location":
            text = row.get("description")
            return text[:1].upper() + text[1:] if text else None
        return row.get(self._kind)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._kind != "room":
            return None
        row = self._row()
        if row.get("status") == "away":
            last = row.get("last_seen")
            return {"floor": None, "confidence": None, "x": None, "y": None, "z": None, "radius_m": None,
                    "verdict": None, "near": None, "ha_area": None,
                    "last_seen": dt_util.utc_from_timestamp(last).isoformat() if last is not None else None,
                    "last_room": row.get("last_room"), "last_floor": row.get("last_floor")}

        def rnd(value: Any, ndigits: int) -> Any:
            return round(value, ndigits) if isinstance(value, (int, float)) else value

        confidence = row.get("p_room")
        return {
            "floor": row.get("floor"),
            "confidence": round(round(confidence / 0.05) * 0.05, 2) if isinstance(confidence, (int, float)) else None,
            "x": rnd(row.get("x"), 1),
            "y": rnd(row.get("y"), 1),
            "z": rnd(row.get("z"), 1),
            "radius_m": rnd(row.get("r68"), 1),
            "verdict": row.get("verdict"),
            "near": row.get("near"),
            "ha_area": self._ha_area(row.get("room")),
        }

    def _ha_area(self, room: str | None) -> str | None:
        """The Home Assistant area with the same name as the room, if there is one (spec section 11)."""
        if not room or self.hass is None:
            return None
        area = ar.async_get(self.hass).async_get_area_by_name(room)
        return area.id if area else None
