"""Home or not_home for every device the engine tracks: Home Assistant's standard presence entity (spec 3.3)."""

from __future__ import annotations

from homeassistant.components.device_tracker import BaseScannerEntity, SourceType
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RtlsConfigEntry
from .entity import device_info
from .runner import BridgeRunner


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
            async_add_entities(RtlsTracker(runner, key) for key in new)

    @callback
    def _forget(key: str) -> None:
        known.discard(key)

    entry.async_on_unload(async_dispatcher_connect(hass, runner.signal, _add_new))
    entry.async_on_unload(async_dispatcher_connect(hass, runner.removed_signal, _forget))
    _add_new()


class RtlsTracker(BaseScannerEntity):
    """`home` while the engine hears the device, `not_home` once it is away. BaseScannerEntity, not ScannerEntity:
    tags are keyed by Bluetooth address or iBeacon identity, not by a network MAC."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_source_type = SourceType.BLUETOOTH_LE

    def __init__(self, runner: BridgeRunner, key: str) -> None:
        self._runner, self._key = runner, key
        self._attr_unique_id = f"{runner.entry.entry_id}-{key}-tracker"
        self._attr_device_info = device_info(runner, key)
        self._written: tuple[bool, bool] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._written = (self.is_connected, self.available)
        self.async_on_remove(async_dispatcher_connect(self.hass, self._runner.signal, self._on_update))

    @callback
    def _on_update(self) -> None:
        current = (self.is_connected, self.available)
        if current != self._written:
            self._written = current
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._runner.available and self._key in self._runner.tracked

    @property
    def is_connected(self) -> bool:
        return (self._runner.tracked.get(self._key) or {}).get("status") != "away"
