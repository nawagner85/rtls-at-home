"""The bridge reports Home Assistant's floors and areas so the engine's map editor can link rooms to them."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import floor_registry as fr

from custom_components.rtls_at_home.places import places


async def test_places_lists_floors_by_level_and_areas_with_their_floor(hass: HomeAssistant) -> None:
    floors, areas = fr.async_get(hass), ar.async_get(hass)
    up = floors.async_create("Upstairs", level=1)
    ground = floors.async_create("Ground", level=0)
    areas.async_create("Kitchen", floor_id=ground.floor_id)
    areas.async_create("Attic")
    p = places(hass)
    names = [f["name"] for f in p["floors"]]
    assert names.index("Ground") < names.index("Upstairs")
    assert {"id": ground.floor_id, "name": "Ground", "level": 0} in p["floors"]
    assert up.floor_id in {f["id"] for f in p["floors"]}
    kitchen = next(a for a in p["areas"] if a["name"] == "Kitchen")
    attic = next(a for a in p["areas"] if a["name"] == "Attic")
    assert kitchen["floor"] == ground.floor_id and attic["floor"] is None
