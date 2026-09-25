"""The runner posts batches and turns replies into sensors."""

from __future__ import annotations

import time
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtls_at_home.const import DOMAIN, STALE_AFTER

from .test_reader import MAC_A, Scanner

URL = "http://192.0.2.10:8765"
KEY = MAC_A.lower()
ROW = {"key": KEY, "name": "Tag 1", "room": "Kitchen", "floor": "Main", "p_room": 0.81, "x": 1.234, "y": 5.678,
       "z": 3.1, "r68": 1.5, "verdict": "CALL", "age": 0.4, "near": "coffee bar",
       "description": "near the coffee bar in the Kitchen"}
SCANNER = Scanner("00:00:5E:00:53:F1", "proxy-1", {MAC_A: (-60, 10.0, None, None)})


async def _setup(hass, aioclient_mock, reply):
    SCANNER.discovered_device_timestamps[MAC_A] = 10.0          # the fake scanner is shared: reset it
    aioclient_mock.post(f"{URL}/api/ingest", json=reply)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=URL, data={"url": URL, "token": "t"})
    entry.add_to_hass(hass)
    with patch("custom_components.rtls_at_home.runner.current_scanners", return_value=[SCANNER]):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _posted(aioclient_mock, i):
    return aioclient_mock.mock_calls[i][2]


async def test_sightings_follow_the_engines_wanted_list(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock, {"wanted": [KEY], "tracked": []})
    runner = entry.runtime_data
    with patch("custom_components.rtls_at_home.runner.current_scanners", return_value=[SCANNER]):
        await runner.tick()
        SCANNER.discovered_device_timestamps[MAC_A] = 10.5
        runner._next_try = 0.0
        await runner.tick()
    first, second = _posted(aioclient_mock, 0), _posted(aioclient_mock, 1)
    assert first["v"] == 1 and first["adverts"] == [] and first["scanners"][0][0] == "00:00:5e:00:53:f1"
    assert "census" in first and "census" not in second
    assert second["adverts"] == [[KEY, "00:00:5e:00:53:f1", -60, 10.5]]


async def test_reply_creates_room_floor_and_location_sensors(hass: HomeAssistant, aioclient_mock) -> None:
    from homeassistant.helpers import area_registry as ar

    kitchen = ar.async_get(hass).async_create("Kitchen")
    entry = await _setup(hass, aioclient_mock, {"wanted": [KEY], "tracked": [ROW]})
    with patch("custom_components.rtls_at_home.runner.current_scanners", return_value=[SCANNER]):
        await entry.runtime_data.tick()
    await hass.async_block_till_done()
    room = hass.states.get("sensor.tag_1_room")
    assert room.state == "Kitchen"
    assert room.attributes["floor"] == "Main" and room.attributes["confidence"] == 0.8
    assert room.attributes["x"] == 1.2 and room.attributes["radius_m"] == 1.5
    assert room.attributes["near"] == "coffee bar" and room.attributes["ha_area"] == kitchen.id
    assert hass.states.get("sensor.tag_1_floor").state == "Main"
    assert hass.states.get("sensor.tag_1_location").state == "Near the coffee bar in the Kitchen"


async def test_engine_outage_backs_off_and_goes_unavailable(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock, {"wanted": [KEY], "tracked": [ROW]})
    runner = entry.runtime_data
    with patch("custom_components.rtls_at_home.runner.current_scanners", return_value=[SCANNER]):
        await runner.tick()
        await hass.async_block_till_done()
        aioclient_mock.clear_requests()
        aioclient_mock.post(f"{URL}/api/ingest", status=500)
        await runner.tick()
        assert runner._backoff == 0.5 and runner._next_try > time.monotonic()
        calls = aioclient_mock.call_count
        await runner.tick()                                   # inside the back-off window: nothing sent
        assert aioclient_mock.call_count == calls
        runner.last_ok -= STALE_AFTER + 1
        runner._next_try = 0.0
        await runner.tick()
        await hass.async_block_till_done()
    assert hass.states.get("sensor.tag_1_room").state == "unavailable"
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/api/ingest", json={"wanted": [KEY], "tracked": [ROW]})
    runner._next_try = 0.0
    with patch("custom_components.rtls_at_home.runner.current_scanners", return_value=[SCANNER]):
        await runner.tick()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.tag_1_room").state == "Kitchen" and runner._backoff == 0.0


async def test_state_writes_are_throttled(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock, {"wanted": [KEY], "tracked": [ROW]})
    runner = entry.runtime_data
    with patch("custom_components.rtls_at_home.runner.current_scanners", return_value=[SCANNER]):
        await runner.tick()
        await hass.async_block_till_done()
        first = hass.states.get("sensor.tag_1_room").last_updated
        aioclient_mock.clear_requests()
        aioclient_mock.post(f"{URL}/api/ingest", json={"wanted": [KEY], "tracked": [dict(ROW, x=2.5)]})
        await runner.tick()
        await hass.async_block_till_done()
    assert hass.states.get("sensor.tag_1_room").last_updated == first          # x moved, same room: held
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/api/ingest", json={"wanted": [KEY], "tracked": [dict(ROW, room="Office")]})
    with patch("custom_components.rtls_at_home.runner.current_scanners", return_value=[SCANNER]):
        await runner.tick()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.tag_1_room").state == "Office"               # room change: written now


async def test_untracked_device_goes_unavailable_and_can_be_deleted(hass: HomeAssistant, aioclient_mock) -> None:
    from homeassistant.helpers import device_registry as dr

    entry = await _setup(hass, aioclient_mock, {"wanted": [KEY], "tracked": [ROW]})
    runner = entry.runtime_data
    with patch("custom_components.rtls_at_home.runner.current_scanners", return_value=[SCANNER]):
        await runner.tick()
        await hass.async_block_till_done()
        aioclient_mock.clear_requests()
        aioclient_mock.post(f"{URL}/api/ingest", json={"wanted": [], "tracked": []})
        await runner.tick()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.tag_1_room").state == "unavailable"
    device = next(d for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
                  if (DOMAIN, KEY) in d.identifiers)
    from custom_components.rtls_at_home import async_remove_config_entry_device

    assert await async_remove_config_entry_device(hass, entry, device) is True
