"""*Show on house maps* (spec 2026-10-02 house renders, decision 8): a config switch per tracked device, created
alongside the device's own map, that decides whether it appears on the floor and house pictures."""

from __future__ import annotations

from homeassistant.components.image import async_get_image
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.rtls_at_home.const import DOMAIN

from .test_ha_ui import IN_KITCHEN, IN_PANTRY, KEY2, reply, state
from .test_image import BOTH, META_R, fetches, setup, tick
from .test_meta import META
from .test_runner_sensor import KEY

TAG1_SWITCH = "switch.tag_1_show_on_house_maps"


def switches(hass, entry):
    return {e.unique_id: e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
            if e.domain == "switch"}


async def test_on_by_default(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, meta=META_R))
    assert KEY not in entry.runtime_data.hidden
    s = state(hass, entry, "switch", f"{KEY}-show_on_maps")
    assert s.state == STATE_ON
    assert s.entity_id == TAG1_SWITCH
    assert s.attributes["friendly_name"] == "Tag 1 Show on house maps"


async def test_no_switches_from_an_engine_without_render(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN))
    assert switches(hass, entry) == {}


async def test_turning_it_off_hides_the_device_from_the_house_map(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, IN_PANTRY, meta=META_R))
    runner = entry.runtime_data
    house = state(hass, entry, "image", "house-map").entity_id
    await async_get_image(hass, house)
    assert fetches(aioclient_mock) == [("/api/ingest/render/house.png", BOTH)]
    switch_eid = state(hass, entry, "switch", f"{KEY2}-show_on_maps").entity_id
    before = hass.states.get(house).state
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch_eid}, blocking=True)
    assert KEY2 in runner.hidden
    assert hass.states.get(switch_eid).state == STATE_OFF
    assert hass.states.get(house).state != before                  # a shown-set change: a new picture at once
    await async_get_image(hass, house)
    assert fetches(aioclient_mock)[-1] == ("/api/ingest/render/house.png", KEY)
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch_eid}, blocking=True)
    assert KEY2 not in runner.hidden
    assert hass.states.get(switch_eid).state == STATE_ON


async def test_restored_off_survives_a_restart(hass: HomeAssistant, aioclient_mock) -> None:
    mock_restore_cache(hass, [State(TAG1_SWITCH, STATE_OFF)])
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, meta=META_R))
    assert hass.states.get(TAG1_SWITCH).state == STATE_OFF
    assert KEY in entry.runtime_data.hidden


async def test_a_removed_devices_switch_goes_with_it(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, IN_PANTRY, meta=META_R))
    runner = entry.runtime_data
    switch_eid = state(hass, entry, "switch", f"{KEY2}-show_on_maps").entity_id
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch_eid}, blocking=True)
    assert KEY2 in runner.hidden                                   # off, and kept off the maps, before it is removed
    await tick(hass, aioclient_mock, entry,
               {"wanted": [KEY], "tracked": [IN_KITCHEN], "removed": [KEY2], "meta": META_R})
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}-{KEY2}-show_on_maps") is None
    assert KEY2 not in runner.hidden                                # the removal, not just an unexercised default


async def test_unavailable_when_the_meta_loses_render(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, meta=META_R))
    eid = state(hass, entry, "switch", f"{KEY}-show_on_maps").entity_id
    assert hass.states.get(eid).state != STATE_UNAVAILABLE
    await tick(hass, aioclient_mock, entry, reply(IN_KITCHEN, meta=META))         # an engine reply without `render`
    assert hass.states.get(eid).state == STATE_UNAVAILABLE
