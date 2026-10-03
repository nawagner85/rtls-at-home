"""*Show on house maps* (spec 2026-10-02 house renders, decision 8): a switch per tracked device that keeps it off
the floor and house pictures (`runner.hidden`) while the device's own *Map* always shows it - the dashboard's choice,
not the engine's. On by default, restores its last state across a restart."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RtlsConfigEntry
from .entity import ChangeWriter, device_info
from .image import render_meta
from .runner import BridgeRunner


async def async_setup_entry(
    hass: HomeAssistant, entry: RtlsConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runner = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add() -> None:
        """A switch per tracked device, once the meta has `render` - the same condition and the same known-set
        pattern as the device maps (image.py's `_add`), so an older engine gets neither."""
        if render_meta(runner) is None:
            return
        new = [key for key in runner.tracked if key not in known]
        if new:
            known.update(new)
            async_add_entities(RtlsShowOnMaps(runner, key) for key in new)

    for signal in (runner.meta_signal, runner.signal):
        entry.async_on_unload(async_dispatcher_connect(hass, signal, _add))

    @callback
    def _forget(key: str) -> None:
        known.discard(key)
        runner.hidden.discard(key)  # the engine removed it on purpose; nothing is left that should stay off the maps

    entry.async_on_unload(async_dispatcher_connect(hass, runner.removed_signal, _forget))
    _add()


class RtlsShowOnMaps(ChangeWriter, SwitchEntity, RestoreEntity):
    """On by default. Off adds the key to `runner.hidden` and sends the runner's update signal, so the floor and
    house pictures see a discrete change (decision 10) and redraw without it; the device's own *Map* is unaffected.
    Restores its last state: an off state puts the key back into `runner.hidden` as the entity is added, before the
    first house picture is due where possible (it may be redrawn once more when the restore lands)."""

    _attr_translation_key = "show_on_maps"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runner: BridgeRunner, key: str) -> None:
        self._init_change(runner, f"{key}-show_on_maps")
        self._key = key
        self._attr_is_on = key not in runner.hidden
        self._attr_device_info = device_info(runner, key)

    @property
    def available(self) -> bool:
        """The same rule as the device's own *Map* (`RtlsDeviceMap`): unavailable without `render` too, so the
        switch does not sit on, inert, after the one thing it controls stops existing."""
        return self._runner.available and render_meta(self._runner) is not None and self._key in self._runner.tracked

    def _write(self, on: bool) -> None:
        self._attr_is_on = on
        (self._runner.hidden.discard if on else self._runner.hidden.add)(self._key)
        self._shown = self._now_shown()
        self.async_write_ha_state()
        async_dispatcher_send(self.hass, self._runner.signal)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._write(False)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_OFF:
            self._write(False)
