"""The house's pictures as image entities (spec 2026-10-02 house renders): a map per tracked device, and one per floor
and one of the house on the engine device; each new only when what it shows changes, fetched once per change."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.components.image import async_get_image
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.rtls_at_home.client import EngineClient, InvalidAuth, NotReady
from custom_components.rtls_at_home.const import DOMAIN, STALE_AFTER

from .test_ha_ui import IN_KITCHEN, IN_PANTRY, KEY2, reply, state
from .test_meta import META
from .test_runner_sensor import KEY, SCANNER, URL, _setup

PNG, PNG2 = b"\x89PNG\r\n\x1a\nfirst", b"\x89PNG\r\n\x1a\nsecond"
RENDER = {"v": 1, "house": "0123456789ab",
          "floors": [{"id": "main", "name": "Main"}, {"id": "upper", "name": "Upper"}]}
META_R = dict(META, render=RENDER)
UPSTAIRS = dict(IN_PANTRY, room="Bedroom", room_id="bedroom", floor="Upper", floor_id="upper", area=None)
BOTH = ",".join(sorted((KEY, KEY2)))


def answer(png, status, warming):
    """A picture route: 503 with Retry-After: 1 for the first `warming` asks (the engine drawing its base layers),
    then `status` with `png`."""
    left = [warming]

    async def later(method, url, data):
        await asyncio.sleep(0)                          # drawing takes a moment: cards asking together overlap
        if left[0]:
            left[0] -= 1
            return AiohttpClientMockResponse(method, url, status=503, json={"error": "still being drawn"},
                                             headers={"Retry-After": "1"})
        return AiohttpClientMockResponse(method, url, status=status, response=png)
    return later


async def tick(hass, aioclient_mock, entry, body, png=PNG, status=200, engine=True, warming=0):
    """One reply from the engine (or none, `engine=False`), with its pictures answering `status` (after `warming`
    503s each)."""
    aioclient_mock.clear_requests()
    if engine:
        aioclient_mock.post(f"{URL}/api/ingest", json=body)
    else:
        aioclient_mock.post(f"{URL}/api/ingest", status=500)
    for path in (f"device/{KEY}.png", f"device/{KEY2}.png", "floor/main.png", "floor/upper.png", "house.png"):
        aioclient_mock.get(f"{URL}/api/ingest/render/{path}", side_effect=answer(png, status, warming))
    runner = entry.runtime_data
    runner._next_try = 0.0
    with patch("custom_components.rtls_at_home.runner.current_scanners", return_value=[SCANNER]):
        await runner.tick()
    await hass.async_block_till_done()


async def setup(hass, aioclient_mock, body):
    entry = await _setup(hass, aioclient_mock, body)
    await tick(hass, aioclient_mock, entry, body)
    return entry


def fetches(aioclient_mock):
    """The pictures asked for since the last tick: (path, show)."""
    return [(c[1].path, c[1].query.get("show")) for c in aioclient_mock.mock_calls if "/render/" in c[1].path]


def images(hass, entry):
    return {e.unique_id: e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
            if e.domain == "image"}


async def test_the_maps_appear_when_the_meta_has_render(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, UPSTAIRS, meta=META_R))
    eid, found = entry.entry_id, images(hass, entry)
    assert set(found) == {f"{eid}-house-map", f"{eid}-floor-main-map", f"{eid}-floor-upper-map", f"{eid}-{KEY}-map",
                          f"{eid}-{KEY2}-map"}
    devices = dr.async_get(hass)
    on = {uid: devices.async_get(e.device_id).identifiers for uid, e in found.items()}
    assert on[f"{eid}-house-map"] == on[f"{eid}-floor-main-map"] == on[f"{eid}-floor-upper-map"] == {(DOMAIN, "engine")}
    assert on[f"{eid}-{KEY}-map"] == {(DOMAIN, KEY)} and on[f"{eid}-{KEY2}-map"] == {(DOMAIN, KEY2)}
    assert state(hass, entry, "image", f"{KEY}-map").entity_id == "image.tag_1_map"
    assert state(hass, entry, "image", f"{KEY}-map").attributes["friendly_name"] == "Tag 1 Map"
    assert state(hass, entry, "image", "house-map").attributes["friendly_name"] == "RTLS@Home engine House map"
    assert state(hass, entry, "image", "floor-upper-map").attributes["friendly_name"] == "RTLS@Home engine Upper map"
    for suffix in ("house-map", "floor-main-map", "floor-upper-map", f"{KEY}-map", f"{KEY2}-map"):
        assert state(hass, entry, "image", suffix).state not in (STATE_UNAVAILABLE, "unknown")   # a picture is due


async def test_no_maps_from_an_engine_without_render(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, meta=META))
    assert images(hass, entry) == {}
    await tick(hass, aioclient_mock, entry, reply(IN_KITCHEN, meta=None))
    assert images(hass, entry) == {}


async def test_a_picture_is_fetched_once_per_change_with_the_shown_keys(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, IN_PANTRY, meta=META_R))
    runner = entry.runtime_data
    house = state(hass, entry, "image", "house-map").entity_id
    two = await asyncio.gather(async_get_image(hass, house), async_get_image(hass, house))
    assert [i.content for i in two] == [PNG, PNG] and two[0].content_type == "image/png"
    assert (await async_get_image(hass, house)).content == PNG
    assert fetches(aioclient_mock) == [("/api/ingest/render/house.png", BOTH)]          # once, however many ask
    call = next(c for c in aioclient_mock.mock_calls if "/render/" in c[1].path)
    assert call[0] == "GET" and call[3]["Authorization"] == "Bearer t"
    await async_get_image(hass, state(hass, entry, "image", "floor-main-map").entity_id)
    await async_get_image(hass, state(hass, entry, "image", f"{KEY}-map").entity_id)
    assert fetches(aioclient_mock)[1:] == [("/api/ingest/render/floor/main.png", BOTH),
                                           (f"/api/ingest/render/device/{KEY}.png", None)]
    before = hass.states.get(house).state
    runner.hidden.add(KEY2)                         # off the house maps (Task 5's switch): a new picture at once
    async_dispatcher_send(hass, runner.signal)
    await hass.async_block_till_done()
    assert hass.states.get(house).state != before
    await async_get_image(hass, house)
    assert fetches(aioclient_mock)[-1] == ("/api/ingest/render/house.png", KEY)


async def test_a_room_change_is_a_new_picture_and_a_small_move_is_not(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, meta=META_R))

    def stamps():
        return [state(hass, entry, "image", s).state for s in (f"{KEY}-map", "floor-main-map", "house-map")]

    first = stamps()
    await tick(hass, aioclient_mock, entry, reply(dict(IN_KITCHEN, x=IN_KITCHEN["x"] + 0.3), meta=META_R))
    assert stamps() == first
    await tick(hass, aioclient_mock, entry, reply(dict(IN_KITCHEN, room="Pantry", room_id="pantry"), meta=META_R))
    second = stamps()
    assert all(a != b for a, b in zip(first, second, strict=True))
    upper = state(hass, entry, "image", "floor-upper-map").state                    # nobody upstairs: no change
    new_house = dict(META, render=dict(RENDER, house="ba9876543210"))
    await tick(hass, aioclient_mock, entry, reply(dict(IN_KITCHEN, room="Pantry", room_id="pantry"), meta=new_house))
    assert all(a != b for a, b in zip(second, stamps(), strict=True))               # a new house: every picture
    assert state(hass, entry, "image", "floor-upper-map").state != upper


async def test_the_last_picture_outlives_an_engine_outage(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, meta=META_R))
    runner = entry.runtime_data
    eid = state(hass, entry, "image", f"{KEY}-map").entity_id
    assert (await async_get_image(hass, eid)).content == PNG
    stamp = hass.states.get(eid).state
    runner.last_ok -= STALE_AFTER + 1
    await tick(hass, aioclient_mock, entry, None, status=500, engine=False)
    assert hass.states.get(eid).state == STATE_UNAVAILABLE
    assert all(hass.states.get(e).state == STATE_UNAVAILABLE for e in hass.states.async_entity_ids("image"))
    await tick(hass, aioclient_mock, entry, reply(IN_KITCHEN, meta=META_R), png=PNG2)
    assert hass.states.get(eid).state == stamp                                      # back, nothing changed
    assert (await async_get_image(hass, eid)).content == PNG and fetches(aioclient_mock) == []
    moved = reply(dict(IN_KITCHEN, room="Pantry", room_id="pantry"), meta=META_R)
    await tick(hass, aioclient_mock, entry, moved, status=500)                     # the picture fails
    assert hass.states.get(eid).state != stamp
    assert (await async_get_image(hass, eid)).content == PNG and len(fetches(aioclient_mock)) == 1
    await tick(hass, aioclient_mock, entry, moved, png=PNG2)
    assert (await async_get_image(hass, eid)).content == PNG2                       # the next ask tries again


NEW_HOUSE = dict(META, render=dict(RENDER, house="ba9876543210"))


class FakeTime:
    """The picture module's clock and sleep: a sleep moves the clock on at once (and lets other tasks run)."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)

    def patch(self, wait: float):
        return patch.multiple("custom_components.rtls_at_home.image", monotonic=self.monotonic, sleep=self.sleep,
                              RENDER_WAIT=wait)

    async def timed(self, hass, eid):
        """The picture an ask gets, and how long (by this clock) it took."""
        start = self.now
        image = await async_get_image(hass, eid)
        return image.content, self.now - start


async def warming_up(hass, aioclient_mock, warming):
    """The house map with its first picture taken, then a new house whose pictures answer 503 `warming` times."""
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, meta=META_R))
    eid = state(hass, entry, "image", "house-map").entity_id
    assert (await async_get_image(hass, eid)).content == PNG
    await tick(hass, aioclient_mock, entry, reply(IN_KITCHEN, meta=NEW_HOUSE), png=PNG2, warming=warming)
    return eid


async def test_a_picture_waits_out_the_engines_warm_up(hass: HomeAssistant, aioclient_mock) -> None:
    """After an Apply the first reply brings the new house and the engine is still drawing its base layers (503,
    Retry-After): the same ask waits and returns the new house's picture."""
    eid, clock = await warming_up(hass, aioclient_mock, warming=1), FakeTime()
    with clock.patch(7.0):
        assert await clock.timed(hass, eid) == (PNG2, 1.0)                     # waited out one Retry-After: 1
    assert len(fetches(aioclient_mock)) == 2                                        # the 503, then the picture
    assert (await async_get_image(hass, eid)).content == PNG2 and len(fetches(aioclient_mock)) == 2


async def test_a_warm_up_past_the_budget_serves_the_last_picture(hass: HomeAssistant, aioclient_mock) -> None:
    eid, clock = await warming_up(hass, aioclient_mock, warming=99), FakeTime()
    with clock.patch(2.5):
        assert await clock.timed(hass, eid) == (PNG, 2.0)
    assert len(fetches(aioclient_mock)) == 3                # at 0, 1 and 2 s; a third wait would overrun the 2.5 s


async def test_asks_waiting_on_one_another_share_the_budget(hass: HomeAssistant, aioclient_mock) -> None:
    """Two cards ask at once while the warm-up outlasts the budget: the second's wait for the first counts against
    its own budget, so neither takes longer than RENDER_WAIT (Home Assistant cancels an ask at 10 s)."""
    eid, clock = await warming_up(hass, aioclient_mock, warming=99), FakeTime()
    with clock.patch(2.0):
        first, second = await asyncio.gather(clock.timed(hass, eid), clock.timed(hass, eid))
    assert first == second == (PNG, 2.0)                    # the last good picture, each within the budget
    assert len(fetches(aioclient_mock)) == 3                # the first ask's 0, 1 and 2 s; the second's budget is spent


async def test_the_client_tells_a_picture_not_ready_yet(hass: HomeAssistant, aioclient_mock) -> None:
    client, path = EngineClient(async_get_clientsession(hass), URL, "t"), "/api/ingest/render/house.png"
    for headers, wait in (({"Retry-After": "3"}, 3.0), (None, 2.0), ({"Retry-After": "soon"}, 2.0)):
        aioclient_mock.clear_requests()
        aioclient_mock.get(URL + path, status=503, headers=headers)
        with pytest.raises(NotReady) as err:
            await client.render(path, {"show": KEY})
        assert err.value.retry_after == wait
    aioclient_mock.clear_requests()
    aioclient_mock.get(URL + path, status=401)
    with pytest.raises(InvalidAuth):
        await client.render(path)


async def test_floor_maps_follow_the_meta(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, meta=META_R))
    attic = {"id": "attic", "name": "Attic"}
    fewer = dict(META, render=dict(RENDER, floors=[RENDER["floors"][0], attic]))
    await tick(hass, aioclient_mock, entry, reply(IN_KITCHEN, meta=fewer))
    assert state(hass, entry, "image", "floor-upper-map").state == STATE_UNAVAILABLE   # kept, not deleted
    assert state(hass, entry, "image", "floor-main-map").state != STATE_UNAVAILABLE
    assert state(hass, entry, "image", "floor-attic-map").attributes["friendly_name"] == "RTLS@Home engine Attic map"
    await tick(hass, aioclient_mock, entry, reply(IN_KITCHEN, meta=META_R))
    assert state(hass, entry, "image", "floor-upper-map").state != STATE_UNAVAILABLE


async def test_a_device_map_follows_its_device(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await setup(hass, aioclient_mock, reply(IN_KITCHEN, meta=META_R))
    await tick(hass, aioclient_mock, entry, reply(IN_KITCHEN, IN_PANTRY, meta=META_R))
    assert state(hass, entry, "image", f"{KEY2}-map").state != STATE_UNAVAILABLE         # a new device: its map
    await tick(hass, aioclient_mock, entry, reply(IN_KITCHEN, meta=META_R))
    assert state(hass, entry, "image", f"{KEY2}-map").state == STATE_UNAVAILABLE         # no longer tracked
