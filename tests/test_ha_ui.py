"""Home Assistant UI (spec 2026-10-01-ha-ui): presence per area, the receivers and the engine as entities, areas by
the map's links."""

from __future__ import annotations

import copy
from unittest.mock import patch

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rtls_at_home.const import DOMAIN, STALE_AFTER

from .test_meta import META, PROXY_MAC
from .test_runner_sensor import ROW, SCANNER, URL, _setup

KEY2 = "00:00:5e:00:53:0b"
IN_KITCHEN = dict(ROW, room_id="kitchen", floor_id="main", area="kitchen", status="present")
IN_PANTRY = dict(ROW, key=KEY2, name="Tag 2", room="Pantry", room_id="pantry", floor_id="main", area="kitchen",
                 status="present", description="in the Pantry")


def reply(*rows, meta=META):
    out = {"wanted": [r["key"] for r in rows], "tracked": list(rows)}
    if meta is not None:
        out["meta"] = meta
    return out


async def tick(hass, aioclient_mock, entry, body):
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/api/ingest", json=body)
    runner = entry.runtime_data
    runner._next_try = 0.0
    with patch("custom_components.rtls_at_home.runner.current_scanners", return_value=[SCANNER]):
        await runner.tick()
    await hass.async_block_till_done()


def state(hass, entry, platform, suffix):
    eid = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}-{suffix}")
    assert eid, f"no {platform} {suffix}"
    return hass.states.get(eid)


async def setup(hass, aioclient_mock, body):
    ar.async_get(hass).async_create("Kitchen")                               # id "kitchen"
    entry = await _setup(hass, aioclient_mock, body)
    await tick(hass, aioclient_mock, entry, body)
    return entry


async def test_an_area_is_occupied_while_a_device_is_in_any_room_linked_to_it(hass: HomeAssistant, aioclient_mock):
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, IN_PANTRY))
    st = state(hass, entry, "binary_sensor", "area-kitchen-presence")
    assert st.state == "on" and st.attributes["devices"] == ["Tag 1", "Tag 2"] and st.attributes["count"] == 2
    assert st.attributes["device_class"] == "occupancy"
    dev = dr.async_get(hass).async_get(er.async_get(hass).async_get(st.entity_id).device_id)
    assert dev.name == "Kitchen presence" and dev.area_id == "kitchen"           # on the Kitchen's page by itself
    presence = [e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
                if e.unique_id.endswith("-presence")]
    assert len(presence) == 1                                                     # the logical Nook has none
    away = [dict(r, status="away", room=None, room_id=None, floor_id=None, area=None) for r in (IN_KITCHEN, IN_PANTRY)]
    await tick(hass, aioclient_mock, entry, reply(*away))
    st = state(hass, entry, "binary_sensor", "area-kitchen-presence")
    assert st.state == "off" and st.attributes["count"] == 0


async def test_presence_writes_only_when_it_changes(hass: HomeAssistant, aioclient_mock):
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN))
    first = state(hass, entry, "binary_sensor", "area-kitchen-presence").last_updated
    await tick(hass, aioclient_mock, entry, reply(dict(IN_KITCHEN, x=2.0)))          # moved within the room
    assert state(hass, entry, "binary_sensor", "area-kitchen-presence").last_updated == first
    await tick(hass, aioclient_mock, entry, reply(IN_KITCHEN, IN_PANTRY))             # a second device arrives
    assert state(hass, entry, "binary_sensor", "area-kitchen-presence").attributes["count"] == 2


async def test_an_area_no_room_links_to_any_more_goes_unavailable(hass: HomeAssistant, aioclient_mock):
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN))
    relinked = copy.deepcopy(META)
    for r in relinked["rooms"]:
        r["area"] = None
    await tick(hass, aioclient_mock, entry, reply(dict(IN_KITCHEN, area=None), meta=relinked))
    assert state(hass, entry, "binary_sensor", "area-kitchen-presence").state == STATE_UNAVAILABLE


async def test_receivers_are_connected_via_their_proxy_and_follow_the_meta(hass: HomeAssistant, aioclient_mock):
    esphome = MockConfigEntry(domain="esphome", title="kitchen-proxy")
    esphome.add_to_hass(hass)
    proxy = dr.async_get(hass).async_get_or_create(config_entry_id=esphome.entry_id, name="kitchen-proxy",
                                                   connections={(dr.CONNECTION_NETWORK_MAC, PROXY_MAC.lower())})
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN))
    heard = state(hass, entry, "binary_sensor", "receiver-Kitchen Proxy-heard")
    assert heard.state == "on" and heard.attributes["device_class"] == "connectivity"
    ent = er.async_get(hass).async_get(heard.entity_id)
    assert ent.entity_category == "diagnostic"
    assert dr.async_get(hass).async_get(ent.device_id).via_device_id == proxy.id
    assert state(hass, entry, "binary_sensor", "receiver-Upper Show-heard").state == "off"       # not alive
    assert state(hass, entry, "sensor", "receiver-Kitchen Proxy-correction").state == "-1.2"
    placed = state(hass, entry, "sensor", "receiver-Kitchen Proxy-placed")
    assert placed.state == "Kitchen" and placed.attributes["floor"] == "Main"
    fewer = dict(META, receivers=META["receivers"][:1])
    await tick(hass, aioclient_mock, entry, reply(IN_KITCHEN, meta=fewer))
    assert state(hass, entry, "binary_sensor", "receiver-Upper Show-heard").state == STATE_UNAVAILABLE


async def test_the_engine_reports_its_status_receivers_and_devices(hass: HomeAssistant, aioclient_mock):
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, IN_PANTRY))
    assert state(hass, entry, "sensor", "engine-status").state == "tracking"
    rh = state(hass, entry, "sensor", "engine-receivers_heard")
    assert rh.state == "1" and rh.attributes["total"] == 2
    assert state(hass, entry, "sensor", "engine-devices_present").state == "2"
    applying = copy.deepcopy(META)
    applying["engine"]["status"] = "applying"
    await tick(hass, aioclient_mock, entry, reply(IN_KITCHEN, meta=applying))
    assert state(hass, entry, "sensor", "engine-status").state == "applying"
    assert state(hass, entry, "sensor", "engine-devices_present").state == "1"
    runner = entry.runtime_data
    runner.last_ok -= STALE_AFTER + 1                                             # the engine went quiet
    async_dispatcher_send(hass, runner.signal)
    await hass.async_block_till_done()
    for platform, suffix in (("sensor", "engine-status"), ("binary_sensor", "area-kitchen-presence"),
                             ("binary_sensor", "receiver-Kitchen Proxy-heard")):
        assert state(hass, entry, platform, suffix).state == STATE_UNAVAILABLE


async def test_an_older_engine_without_meta_gets_no_new_entities(hass: HomeAssistant, aioclient_mock):
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, meta=None))
    ours = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert {e.unique_id.rsplit("-", 1)[-1] for e in ours} == {"room", "floor", "location", "tracker"}


async def test_the_room_sensors_area_is_the_maps_link(hass: HomeAssistant, aioclient_mock):
    other = ar.async_get(hass).async_create("Cooking")
    await setup(hass, aioclient_mock, reply(dict(IN_KITCHEN, area=other.id)))
    assert hass.states.get("sensor.tag_1_room").attributes["ha_area"] == other.id      # not the same-name Kitchen


async def test_an_unlinked_rooms_area_is_the_same_name_area(hass: HomeAssistant, aioclient_mock):
    await setup(hass, aioclient_mock, reply(dict(IN_KITCHEN, area=None)))
    assert hass.states.get("sensor.tag_1_room").attributes["ha_area"] == "kitchen"


async def test_a_reload_brings_the_same_entities_back(hass: HomeAssistant, aioclient_mock):
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, IN_PANTRY))
    before = sorted(e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await tick(hass, aioclient_mock, entry, reply(IN_KITCHEN, IN_PANTRY))
    after = sorted(e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id))
    assert after == before and state(hass, entry, "binary_sensor", "area-kitchen-presence").state == "on"


async def test_an_area_deleted_in_home_assistant_is_not_recreated(hass: HomeAssistant, aioclient_mock):
    """The map still links rooms to "kitchen", but there is no such area: no presence, and no area made up."""
    entry = await _setup(hass, aioclient_mock, reply(IN_KITCHEN))
    await tick(hass, aioclient_mock, entry, reply(IN_KITCHEN))
    uid = f"{entry.entry_id}-area-kitchen-presence"
    assert er.async_get(hass).async_get_entity_id("binary_sensor", DOMAIN, uid) is None
    assert ar.async_get(hass).async_get_area("kitchen") is None and not ar.async_get(hass).async_list_areas()
    ar.async_get(hass).async_create("Kitchen")                               # it comes back
    await tick(hass, aioclient_mock, entry, reply(IN_KITCHEN))
    assert state(hass, entry, "binary_sensor", "area-kitchen-presence").state == "on"


async def test_a_new_households_engine_reports_setup_until_its_house_is_ready(hass: HomeAssistant, aioclient_mock):
    """A3 setup mode: no engine yet (the house has no rooms or no placed receiver) - nothing tracked, no rooms."""
    in_setup = dict(META, engine=dict(build=None, status="setup"), rooms=[], receivers=[])
    entry = await setup(hass, aioclient_mock, reply(meta=in_setup))
    assert state(hass, entry, "sensor", "engine-status").state == "setup"
    assert state(hass, entry, "sensor", "engine-receivers_heard").state == "0"
    await tick(hass, aioclient_mock, entry, reply(IN_KITCHEN))
    assert state(hass, entry, "sensor", "engine-status").state == "tracking"
