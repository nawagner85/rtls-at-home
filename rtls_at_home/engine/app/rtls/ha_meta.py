"""What the engine tells Home Assistant besides positions (spec docs/specs/2026-10-01-ha-ui-design.md): the map's
rooms and the Home Assistant areas they link to, the receivers with the addresses Home Assistant knows them by, and
whether the engine is tracking or applying a new house. Pure: the server feeds it the house document and its rows."""
import house_apply as HAP

APPLY_STEPS = {k for k, _ in HAP.STEPS}


def room_index(doc):
    """{(floor id, room name): {"room_id", "area"}}: how a position's floor and room name (what the model calls a
    room) find the map's room and its Home Assistant area (None for a logical room)."""
    return {(f["id"], r.get("name")): {"room_id": r["id"], "area": r.get("ha_area") or None}
            for f in doc.get("floors") or [] for r in f.get("rooms") or [] if r.get("name")}


def rooms(doc):
    """The map's named rooms, bottom floor first: [{id, name, floor_id, floor, area}]."""
    return [{"id": r["id"], "name": r["name"], "floor_id": f["id"], "floor": f.get("name"), "area": r.get("ha_area") or None}
            for f in doc.get("floors") or [] for r in f.get("rooms") or [] if r.get("name")]


def receivers(rows, doc):
    """The receivers' rows (GET /api/receivers) with the addresses the house records for them: `mac` (an ESPHome
    proxy's Wi-Fi MAC) and `address` (the Bluetooth source Home Assistant reports); None where it records none."""
    by_name = {r.get("name"): r for r in doc.get("receivers") or []}
    return [dict(row, mac=(by_name.get(row.get("name")) or {}).get("mac"),
                 address=(by_name.get(row.get("name")) or {}).get("address")) for row in rows]


def status(apply_state, idle="tracking"):
    """"applying" while an apply runs one of its steps, else `idle` ("tracking"; "setup" with no engine yet)."""
    return "applying" if apply_state in APPLY_STEPS else idle
