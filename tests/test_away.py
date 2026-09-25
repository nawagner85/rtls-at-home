"""Away rows, the device tracker, deliberate removals and renames."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.rtls_at_home.const import DOMAIN
from custom_components.rtls_at_home.sensor import away_sentence

from .test_runner_sensor import KEY, ROW, SCANNER, URL, _setup

PT = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 9, 25, 8, 0, tzinfo=PT)
AT_612 = datetime(2026, 9, 25, 6, 12, tzinfo=PT).timestamp()
YESTERDAY_2340 = datetime(2026, 9, 24, 23, 40, tzinfo=PT).timestamp()
AWAY_ROW = dict(ROW, room=None, floor=None, p_room=None, x=None, y=None, z=None, r68=None, verdict=None, near=None,
                description=None, status="away", last_seen=AT_612, last_room="Living Room", last_floor="Main")


def test_away_sentence_formats() -> None:
    assert away_sentence(AT_612, "Living Room", NOW) == "Not detected since 6:12 AM, last in the Living Room"
    assert away_sentence(YESTERDAY_2340, "Owner's Suite", NOW) == \
        "Not detected since Sep 24, 11:40 PM, last in the Owner's Suite"
    assert away_sentence(AT_612, None, NOW) == "Not detected since 6:12 AM"
    assert away_sentence(None, None, NOW) == "Not detected"


async def _tick(hass, entry, reply, aioclient_mock) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/api/ingest", json=reply)
    entry.runtime_data._next_try = 0.0
    with patch("custom_components.rtls_at_home.runner.current_scanners", return_value=[SCANNER]):
        await entry.runtime_data.tick()
    await hass.async_block_till_done()


async def test_away_row_renders_with_persisted_last_seen(hass: HomeAssistant, aioclient_mock) -> None:
    await hass.config.async_set_time_zone("America/Los_Angeles")
    entry = await _setup(hass, aioclient_mock, {"wanted": [KEY], "tracked": [AWAY_ROW]})
    await _tick(hass, entry, {"wanted": [KEY], "tracked": [AWAY_ROW]}, aioclient_mock)
    room = hass.states.get("sensor.tag_1_room")
    assert room.state == "Away" and room.attributes["last_room"] == "Living Room"
    assert room.attributes["last_seen"].startswith("2026-09-25T13:12:00")          # 6:12 AM PDT, in UTC
    assert room.attributes["x"] is None and room.attributes["last_floor"] == "Main"
    assert hass.states.get("sensor.tag_1_floor").state == "Away"
    assert hass.states.get("sensor.tag_1_location").state.startswith("Not detected since ")
    assert hass.states.get("sensor.tag_1_location").state.endswith(", last in the Living Room")
    assert hass.states.get("device_tracker.tag_1").state == "not_home"
    await _tick(hass, entry, {"wanted": [KEY], "tracked": [dict(ROW, status="present")]}, aioclient_mock)
    assert hass.states.get("sensor.tag_1_room").state == "Kitchen"
    assert hass.states.get("device_tracker.tag_1").state == "home"


async def test_old_engine_reply_behaves_like_0_1_0(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock, {"wanted": [KEY], "tracked": [ROW]})
    await _tick(hass, entry, {"wanted": [KEY], "tracked": [ROW]}, aioclient_mock)
    assert hass.states.get("sensor.tag_1_room").state == "Kitchen"
    assert hass.states.get("device_tracker.tag_1").state == "home"
    assert entry.runtime_data.removed == set() and entry.runtime_data.detail is None
    reg = dr.async_get(hass)
    assert any((DOMAIN, KEY) in d.identifiers for d in dr.async_entries_for_config_entry(reg, entry.entry_id))


async def test_removed_device_is_deleted_and_can_come_back(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock, {"wanted": [KEY], "tracked": [ROW]})
    await _tick(hass, entry, {"wanted": [KEY], "tracked": [ROW]}, aioclient_mock)
    await _tick(hass, entry, {"wanted": [], "tracked": [], "removed": [KEY]}, aioclient_mock)
    reg = dr.async_get(hass)
    assert not [d for d in dr.async_entries_for_config_entry(reg, entry.entry_id) if (DOMAIN, KEY) in d.identifiers]
    assert hass.states.get("sensor.tag_1_room") is None and hass.states.get("device_tracker.tag_1") is None
    await _tick(hass, entry, {"wanted": [KEY], "tracked": [ROW], "removed": []}, aioclient_mock)
    assert hass.states.get("sensor.tag_1_room").state == "Kitchen"
    assert hass.states.get("device_tracker.tag_1").state == "home"


async def test_rename_follows_the_engine_but_not_over_a_name_set_in_ha(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await _setup(hass, aioclient_mock, {"wanted": [KEY], "tracked": [ROW]})
    await _tick(hass, entry, {"wanted": [KEY], "tracked": [ROW]}, aioclient_mock)
    await _tick(hass, entry, {"wanted": [KEY], "tracked": [dict(ROW, name="Keys")]}, aioclient_mock)
    reg = dr.async_get(hass)
    dev = next(d for d in dr.async_entries_for_config_entry(reg, entry.entry_id) if (DOMAIN, KEY) in d.identifiers)
    assert dev.name == "Keys"
    reg.async_update_device(dev.id, name_by_user="My keys")
    await _tick(hass, entry, {"wanted": [KEY], "tracked": [dict(ROW, name="Spare keys")]}, aioclient_mock)
    dev = reg.async_get(dev.id)
    assert dev.name == "Spare keys" and dev.name_by_user == "My keys"
