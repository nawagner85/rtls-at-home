"""Home Assistant's floors and areas, sent with the census so the engine's map editor can link the rooms and floors
drawn in its house map to them (a room either is a Home Assistant area or a logical room of the map)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import floor_registry as fr


def places(hass: HomeAssistant) -> dict[str, Any]:
    """{"floors": [{id, name, level}] bottom first, "areas": [{id, name, floor}] by name}."""
    floors = [{"id": f.floor_id, "name": f.name, "level": f.level} for f in fr.async_get(hass).async_list_floors()]
    floors.sort(key=lambda f: (f["level"] is None, f["level"] if f["level"] is not None else 0, f["name"].lower()))
    areas = [{"id": a.id, "name": a.name, "floor": a.floor_id} for a in ar.async_get(hass).async_list_areas()]
    areas.sort(key=lambda a: a["name"].lower())
    return {"floors": floors, "areas": areas}
