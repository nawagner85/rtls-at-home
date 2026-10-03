"""When a picture of the house is new (spec 2026-10-02 house renders, decision 10): at once for a room, floor, status,
name (decision 11: names are on the pictures) or shown-set change or a new house; for a pin moving (or its radius
changing) at least 0.5 m, at most every 10 s. Dashboards refetch a picture only when it is new. Pure: no Home Assistant
here."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .const import WRITE_EVERY

type Pin = tuple[Any, Any, Any, Any, Any, Any, Any]  # (status, floor_id, room, x, y, r68, name)


@dataclass(frozen=True)
class Snapshot:
    """What a picture shows: a pin per device key it draws, and the house it is drawn on (`meta.render.house`)."""

    pins: dict[str, Pin]
    house: str | None


def snapshot(rows: dict[str, dict[str, Any]], house: str | None) -> Snapshot:
    """The snapshot of the tracked rows (by key) a picture draws."""
    return Snapshot({k: (r.get("status"), r.get("floor_id"), r.get("room"), r.get("x"), r.get("y"), r.get("r68"),
                         r.get("name")) for k, r in rows.items()}, house)


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _discrete(pin: Pin) -> tuple:
    """The part of a pin that changes a picture at once: status, floor, room, the engine's name for the device, and
    whether it has a position and a radius at all (an estimate appearing is a change, not a move)."""
    status, floor, room, x, y, r68, name = pin
    return status, floor, room, name, _number(x) and _number(y), _number(r68)


def _moved(pin: Pin, last: Pin) -> float:
    """How far the pin moved, or its radius changed, in metres since `last` (0 for what has no number)."""
    x, y, r68 = pin[3:6]
    lx, ly, lr = last[3:6]
    step = math.hypot(x - lx, y - ly) if all(map(_number, (x, y, lx, ly))) else 0.0
    return max(step, abs(r68 - lr) if _number(r68) and _number(lr) else 0.0)


class Clock:
    """One per picture: `due` says whether a picture of `snapshot` would be new; `taken` records the one marked."""

    def __init__(self, min_every: float = WRITE_EVERY, move: float = 0.5) -> None:
        self.min_every, self.move = min_every, move
        self._last: Snapshot | None = None
        self._at = float("-inf")

    def due(self, snap: Snapshot, now: float) -> bool:
        last = self._last
        if last is None or snap.house != last.house or snap.pins.keys() != last.pins.keys():
            return True
        if any(_discrete(pin) != _discrete(last.pins[k]) for k, pin in snap.pins.items()):
            return True
        if now - self._at < self.min_every:
            return False
        return any(_moved(pin, last.pins[k]) >= self.move for k, pin in snap.pins.items())

    def taken(self, snap: Snapshot, now: float) -> None:
        self._last, self._at = snap, now
