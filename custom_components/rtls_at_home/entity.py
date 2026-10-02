"""The Home Assistant devices RTLS@Home's entities belong to: each tracked device, the engine, an area's presence,
and each receiver (connected via its ESPHome proxy's device when Home Assistant has one)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN

if TYPE_CHECKING:
    from .runner import BridgeRunner


def device_info(runner: BridgeRunner, key: str) -> DeviceInfo:
    row = runner.tracked.get(key, {})
    return DeviceInfo(identifiers={(DOMAIN, key)}, name=row.get("name") or key, manufacturer="RTLS@Home",
                      model="Tracked device", configuration_url=runner.client.url)


def engine_device(runner: BridgeRunner) -> DeviceInfo:
    """The engine: its status, the receivers it hears, the devices present; "Visit" opens its page."""
    build = ((runner.meta or {}).get("engine") or {}).get("build")
    return DeviceInfo(identifiers={(DOMAIN, "engine")}, name="RTLS@Home engine", manufacturer="RTLS@Home",
                      model="Engine", sw_version=build, configuration_url=runner.client.url)


def area_device(area_id: str, area_name: str) -> DeviceInfo:
    """A Home Assistant area's presence, suggested into that area so it shows on the area's page."""
    return DeviceInfo(identifiers={(DOMAIN, f"area:{area_id}")}, name=f"{area_name} presence", manufacturer="RTLS@Home",
                      model="Area presence", suggested_area=area_name)


def _connections(rx: dict[str, Any]) -> list[tuple[str, str]]:
    """The receiver's Wi-Fi MAC and Bluetooth address in every spelling a device registry entry may use."""
    out = []
    for kind, value in ((dr.CONNECTION_NETWORK_MAC, rx.get("mac")), (dr.CONNECTION_BLUETOOTH, rx.get("address"))):
        if value:
            for v in dict.fromkeys((value, value.lower(), value.upper(), dr.format_mac(value))):
                out.append((kind, v))
    return out


def receiver_device(hass: HomeAssistant, rx: dict[str, Any]) -> DeviceInfo:
    """The receiver's own device, named after it and connected via the Home Assistant device that has its Wi-Fi MAC
    or Bluetooth address (its ESPHome proxy), so each page links to the other. Home Assistant 2026.9 keeps devices
    per integration: identifiers and connections are not merged across integrations any more."""
    info = DeviceInfo(identifiers={(DOMAIN, f"receiver:{rx['name']}")}, name=rx["name"], manufacturer="RTLS@Home",
                      model="Receiver")
    conns = set(_connections(rx))
    if conns:
        parent = next(iter(dr.async_get(hass).async_get_devices(connections=conns)), None)
        if parent is not None:
            info["via_device_id"] = parent.id
    return info


class ChangeWriter:
    """Mixin for the entities built from the meta and the tracked rows: listens to every reply and every meta, and
    writes its state only when what it shows changes (availability, state, attributes) - never at the poll rate."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def _init_change(self, runner: BridgeRunner, suffix: str) -> None:
        self._runner = runner
        self._attr_unique_id = f"{runner.entry.entry_id}-{suffix}"
        self._shown: tuple | None = None

    def _now_shown(self) -> tuple:
        return (self.available, self.state if self.available else None, repr(self.extra_state_attributes))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._shown = self._now_shown()
        for signal in (self._runner.signal, self._runner.meta_signal):
            self.async_on_remove(async_dispatcher_connect(self.hass, signal, self._maybe_write))

    @callback
    def _maybe_write(self) -> None:
        now = self._now_shown()
        if now != self._shown:
            self._shown = now
            self.async_write_ha_state()


def meta_receiver(runner: BridgeRunner, name: str) -> dict[str, Any] | None:
    """The receiver as the engine's latest meta lists it, or None."""
    return next((r for r in (runner.meta or {}).get("receivers") or [] if r.get("name") == name), None)


def linked_areas(runner: BridgeRunner) -> set[str]:
    """The Home Assistant areas the map's rooms link to, in the latest meta."""
    return {r["area"] for r in (runner.meta or {}).get("rooms") or [] if r.get("area")}
