"""When a picture of the house is new (spec 2026-10-02 house renders, decision 10): the clock, pure."""

from __future__ import annotations

from custom_components.rtls_at_home.pictures import Clock, Snapshot, snapshot

KEY, KEY2 = "00:00:5e:00:53:0a", "00:00:5e:00:53:0b"
HOUSE = "0123456789ab"


def at(x=1.0, y=2.0, room="Kitchen", r68=1.5, status="present", floor="main", house=HOUSE, name="Tag 1"):
    return Snapshot({KEY: (status, floor, room, x, y, r68, name)}, house)


def pictured(snap, now=0.0):
    clock = Clock()
    assert clock.due(snap, now)
    clock.taken(snap, now)
    return clock


def test_the_first_call_is_due():
    assert Clock().due(at(), 0.0)
    assert Clock().due(Snapshot({}, None), 0.0)                    # even with nothing on it


def test_the_same_snapshot_is_not_due():
    clock = pictured(at())
    assert not clock.due(at(), 0.1) and not clock.due(at(), 1000.0)


def test_a_room_floor_or_status_change_is_due_at_once():
    clock = pictured(at())
    assert clock.due(at(room="Pantry"), 0.1)
    assert clock.due(at(floor="upper"), 0.1)
    assert clock.due(at(status="away", x=None, y=None, r68=None), 0.1)


def test_a_small_move_is_never_due():
    clock = pictured(at())
    assert not clock.due(at(x=1.3), 0.1) and not clock.due(at(x=1.3), 1000.0)
    assert not clock.due(at(r68=1.8), 1000.0)


def test_a_move_of_half_a_metre_waits_for_ten_seconds():
    clock = pictured(at())
    assert not clock.due(at(x=1.6), 9.9)
    assert clock.due(at(x=1.6), 10.0)
    clock = pictured(at())
    assert not clock.due(at(r68=2.1), 5.0) and clock.due(at(r68=2.1), 10.0)     # the radius as well


def test_moves_are_measured_from_the_last_picture():
    clock = pictured(at())
    clock.taken(at(x=1.3), 20.0)                                    # say a room change took a picture here
    assert not clock.due(at(x=1.6), 40.0)                           # 0.3 m from that picture
    assert clock.due(at(x=1.8), 40.0)


def test_a_new_house_is_due_at_once():
    clock = pictured(at())
    assert clock.due(at(house="ba9876543210"), 0.1)


def test_a_key_appearing_or_leaving_is_due_at_once():
    clock = pictured(at())
    assert clock.due(Snapshot({**at().pins, KEY2: ("present", "main", "Pantry", 3.0, 1.0, 1.0, "Tag 2")}, HOUSE), 0.1)
    assert clock.due(Snapshot({}, HOUSE), 0.1)


def test_an_estimate_appearing_is_due_at_once():
    clock = pictured(at(x=None, y=None, r68=None, room=None, floor=None))
    assert clock.due(at(x=None, y=None, r68=None), 0.1)            # a room before a position: still a change
    clock = pictured(at(x=None, y=None, r68=None))
    assert clock.due(at(), 0.1)


def test_a_rename_in_the_engine_is_due_at_once():
    clock = pictured(at())                                          # names are on the pictures (decision 11)
    assert clock.due(at(name="Spare Keys"), 0.1)


def test_a_snapshot_reads_the_tracked_rows():
    row = {"key": KEY, "name": "Tag 1", "status": "present", "floor_id": "main", "floor": "Main", "room": "Kitchen",
           "x": 1.0, "y": 2.0, "z": 1.1, "r68": 1.5, "p_room": 0.8}
    assert snapshot({KEY: row}, HOUSE) == at()
