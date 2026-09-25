"""RTLS@Home: streams Home Assistant's Bluetooth sightings to an RTLS@Home engine and exposes its answers."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import EngineClient
from .const import CONF_TOKEN, CONF_URL
from .runner import BridgeRunner

PLATFORMS = [Platform.SENSOR]

type RtlsConfigEntry = ConfigEntry[BridgeRunner]


async def async_setup_entry(hass: HomeAssistant, entry: RtlsConfigEntry) -> bool:
    """Start the bridge for one engine."""
    client = EngineClient(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN])
    runner = BridgeRunner(hass, entry, client)
    entry.runtime_data = runner
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runner.start()
    entry.async_on_unload(runner.stop)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: RtlsConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RtlsConfigEntry) -> bool:
    """Stop the bridge."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: RtlsConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Let the user delete a device the engine no longer tracks (Settings → Devices → Delete)."""
    return True
