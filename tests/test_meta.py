"""The engine's meta (protocol: `meta` on request) and the devices it brings: the engine, an area's presence, the
receivers on their own (ESPHome) devices."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtls_at_home.const import DOMAIN
from custom_components.rtls_at_home.entity import area_device, device_info, engine_device, receiver_device

from .test_runner_sensor import KEY, ROW, SCANNER, URL, _posted, _setup

PROXY_MAC = "00:00:5E:00:53:01"
SHOW_ADDR = "00:00:5E:00:53:02"
META = {
    "engine": {"build": "0123456789", "status": "tracking"},
    "rooms": [{"id": "kitchen", "name": "Kitchen", "floor_id": "main", "floor": "Main", "area": "kitchen"},
              {"id": "pantry", "name": "Pantry", "floor_id": "main", "floor": "Main", "area": "kitchen"},
              {"id": "nook", "name": "Nook", "floor_id": "main", "floor": "Main", "area": None}],
    "receivers": [{"name": "Kitchen Proxy", "kind": "proxy", "enabled": True, "alive": True, "room": "Kitchen",
                   "floor": 1, "floor_name": "Main", "correction": -1.24, "mac": PROXY_MAC, "address": None},
                  {"name": "Upper Show", "kind": "show", "enabled": True, "alive": False, "room": "Nook",
                   "floor": 1, "floor_name": "Main", "correction": None, "mac": None, "address": SHOW_ADDR}],
}


async def test_the_census_asks_for_the_meta_and_the_runner_keeps_it(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock, {"wanted": [KEY], "tracked": [ROW], "meta": META})
    runner = entry.runtime_data
    seen = []
    entry.async_on_unload(async_dispatcher_connect(hass, runner.meta_signal, lambda: seen.append(1)))
    with patch("custom_components.rtls_at_home.runner.current_scanners", return_value=[SCANNER]):
        await runner.tick()
        runner._next_try = 0.0
        await runner.tick()
    assert _posted(aioclient_mock, 0)["meta"] is True and "meta" not in _posted(aioclient_mock, 1)
    assert runner.meta == META and seen


async def test_an_engine_without_meta_leaves_it_none(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock, {"wanted": [KEY], "tracked": [ROW]})
    with patch("custom_components.rtls_at_home.runner.current_scanners", return_value=[SCANNER]):
        await entry.runtime_data.tick()
    assert entry.runtime_data.meta is None


async def test_a_receivers_device_is_connected_via_its_esphome_device(hass: HomeAssistant, aioclient_mock) -> None:
    """Home Assistant 2026.9 keeps devices per integration: the receiver's own device links to the proxy's."""
    esphome = MockConfigEntry(domain="esphome", title="kitchen-proxy")
    esphome.add_to_hass(hass)
    reg = dr.async_get(hass)
    proxy = reg.async_get_or_create(config_entry_id=esphome.entry_id, name="kitchen-proxy", manufacturer="Espressif",
                                    connections={(dr.CONNECTION_NETWORK_MAC, PROXY_MAC.lower())})
    entry = await _setup(hass, aioclient_mock, {"wanted": [], "tracked": [], "meta": META})
    info = receiver_device(hass, META["receivers"][0])                  # the engine writes the MAC in upper case
    assert info["name"] == "Kitchen Proxy" and (DOMAIN, "receiver:Kitchen Proxy") in info["identifiers"]
    assert info["via_device_id"] == proxy.id
    dev = reg.async_get_or_create(config_entry_id=entry.entry_id, **info)
    assert dev.id != proxy.id and dev.via_device_id == proxy.id and reg.async_get(proxy.id).name == "kitchen-proxy"


async def test_a_receiver_without_a_device_of_its_own_stands_alone(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock, {"wanted": [], "tracked": [], "meta": META})
    info = receiver_device(hass, META["receivers"][1])
    assert info["name"] == "Upper Show" and info["model"] == "Receiver" and "via_device_id" not in info


async def test_the_engine_and_the_tracked_devices_link_to_the_engines_page(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock, {"wanted": [KEY], "tracked": [ROW], "meta": META})
    runner = entry.runtime_data
    with patch("custom_components.rtls_at_home.runner.current_scanners", return_value=[SCANNER]):
        await runner.tick()
    eng = engine_device(runner)
    assert eng["identifiers"] == {(DOMAIN, "engine")} and eng["configuration_url"] == URL
    assert eng["sw_version"] == "0123456789" and eng["name"] == "RTLS@Home engine"
    assert device_info(runner, KEY)["configuration_url"] == URL
    area = area_device("kitchen", "Kitchen")
    assert area["identifiers"] == {(DOMAIN, "area:kitchen")} and area["suggested_area"] == "Kitchen"
    assert area["name"] == "Kitchen presence"
