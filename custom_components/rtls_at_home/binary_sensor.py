"""Presence per Home Assistant area, and whether the engine hears each receiver (spec 2026-10-01-ha-ui)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RtlsConfigEntry
from .entity import ChangeWriter, area_device, linked_areas, meta_receiver, receiver_device
from .runner import BridgeRunner


async def async_setup_entry(
    hass: HomeAssistant, entry: RtlsConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runner = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def _add() -> None:
        if not runner.meta:
            return
        new: list[BinarySensorEntity] = []
        areas = ar.async_get(hass)
        for area_id in sorted(linked_areas(runner)):
            area = areas.async_get_area(area_id)
            if area is None:  # deleted in Home Assistant: not recreated from the map's link; back when the area is
                continue
            if ("area", area_id) not in known:
                known.add(("area", area_id))
                new.append(AreaPresence(runner, area_id, area.name))
        for rx in runner.meta.get("receivers") or []:
            if rx.get("name") and ("rx", rx["name"]) not in known:
                known.add(("rx", rx["name"]))
                new.append(ReceiverHeard(runner, rx["name"], receiver_device(hass, rx)))
        if new:
            async_add_entities(new)

    entry.async_on_unload(async_dispatcher_connect(hass, runner.meta_signal, _add))
    _add()


class AreaPresence(ChangeWriter, BinarySensorEntity):
    """On while a tracked device is present in any map room linked to the area; the area's own device carries it,
    suggested into the area, so it shows on the area's page."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_name = None

    def __init__(self, runner: BridgeRunner, area_id: str, area_name: str) -> None:
        self._init_change(runner, f"area-{area_id}-presence")
        self._area = area_id
        self._attr_device_info = area_device(area_id, area_name)

    def _here(self) -> list[str]:
        return sorted(r.get("name") or k for k, r in self._runner.tracked.items()
                      if r.get("status") != "away" and r.get("area") == self._area)

    @property
    def available(self) -> bool:
        return self._runner.available and self._area in linked_areas(self._runner)

    @property
    def is_on(self) -> bool:
        return bool(self._here())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        here = self._here()
        return {"devices": here, "count": len(here)}


class ReceiverHeard(ChangeWriter, BinarySensorEntity):
    """On while the engine hears the receiver and has it switched on."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "heard"

    def __init__(self, runner: BridgeRunner, name: str, device: DeviceInfo) -> None:
        self._init_change(runner, f"receiver-{name}-heard")
        self._name = name
        self._attr_device_info = device

    @property
    def available(self) -> bool:
        return self._runner.available and meta_receiver(self._runner, self._name) is not None

    @property
    def is_on(self) -> bool:
        rx = meta_receiver(self._runner, self._name) or {}
        return bool(rx.get("alive")) and rx.get("enabled", True) is not False
