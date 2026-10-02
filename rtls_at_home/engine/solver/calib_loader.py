"""Calibration rounds from the in-app Calibrate tab -> solver events.

A round file (written by app/rtls/calib.py) holds one capture: where each tag stood and what every
scanner heard. Each (round, tag) becomes one event in the same shape bakeoff.events() produces:

  group    - the held-out unit: a pole's tags share one group (held out together, so the three
             heights on one pole never leak into each other's test); a loose tag is its own group
  round    - the round id: convergence.py holds out whole rounds
  ogroup   - one transmit offset per physical tag, shared with the older tag rounds (tag1/tag2/tag3)
  validation / co_location - never trained on (see trainable)

Where rounds are read from (plan 2026-10-01-a4): RP_CALIB_DIR, else the server's data folder (<RTLS_SESSIONS>/calib,
where the Calibrate tab writes rounds/), else - a development run - the repo's curated copy (survey/calib/). The fresh
survey (fresh_data.py) reads its own folder's rounds and adds the data folder's newer ones by round id.
"""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIR = os.path.join(HERE, "..", "survey", "calib")


def added_dir():
    """The rounds an installation captured itself: RP_CALIB_DIR, else the data folder's calib/ (RTLS_SESSIONS); None
    for a development run outside a data folder."""
    if os.environ.get("RP_CALIB_DIR"):
        return os.environ["RP_CALIB_DIR"]
    s = os.environ.get("RTLS_SESSIONS")
    return os.path.join(s, "calib") if s else None


def default_dir():
    """Where rounds are read from: the installation's own (added_dir), else the repo's curated copy."""
    return added_dir() or DEFAULT_DIR


def load_rounds(calib_dir):
    """Every readable round file in calib_dir and calib_dir/rounds, sorted by round id."""
    out = []
    for fn in glob.glob(os.path.join(calib_dir, "*.json")) + glob.glob(os.path.join(calib_dir, "rounds", "*.json")):
        try:
            r = json.load(open(fn, encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"  [calib] skipping unreadable {os.path.basename(fn)}: {e}", file=sys.stderr)
            continue
        if isinstance(r, dict) and r.get("id") and r.get("placements"):
            out.append(r)
    return sorted(out, key=lambda r: r["id"])


def _floor_of(p, house):
    """A placement's floor index: by the floor's id when the round kept one and the house has it (a floor added below
    does not move it), else the index it kept; None when the house has no such floor."""
    ids = list(getattr(house, "ids", None) or [])
    if p.get("floor_id") in ids:
        return ids.index(p["floor_id"])
    fi = int(p["floor"])
    return fi if 0 <= fi < len(house.Z) else None


def _room_of(p, house, fi):
    """The room a round names, or - when the house no longer has it on that floor (renamed, redrawn) - the room the
    tag stands in now."""
    room = p.get("room")
    floors = getattr(house, "floors", None)
    names = floors[fi].get("names") if floors and isinstance(floors[fi], dict) else None
    if room is None or names is None or room in names.values():
        return room
    return house.room_at(fi, float(p["x"]), float(p["y"]))


def round_events(rnd, house):
    """The round's events; a placement the house cannot place (its floor is gone, a field is missing) is left out
    with a line - a house edited after a capture must never stop the engine (A4 final review)."""
    rid, flags = rnd["id"], rnd.get("flags") or {}
    data = rnd.get("data") or {}
    out = []
    for p in rnd["placements"]:
        tag = p.get("tag")
        obs = data.get(tag)
        if not obs:
            continue
        try:
            fi = _floor_of(p, house)
            if fi is None:
                raise ValueError(f"floor {p.get('floor_id') or p.get('floor')} is not in the house")
            room = _room_of(p, house, fi)
            if room != p.get("room"):
                print(f"  [calib] {rid}/{tag}: room {p.get('room')!r} is gone; it stands in {room!r}", file=sys.stderr)
            unit = p.get("pole") if rnd.get("type") == "pole" and p.get("pole") else tag
            out.append(dict(
                id=f"{rid}/{tag}", group=f"{rid}/{unit}", round=rid, slot=p.get("slot"),
                kind="anchor", ogroup=f"tag:{tag}",
                xy=(float(p["x"]), float(p["y"])), fi=fi, h=float(p["z_abs"]) - float(house.Z[fi]),
                room=room, t=float(rnd.get("t0") or 0.0),
                obs={s: dict(med=float(v["med"]), n=int(v["n"])) for s, v in obs.items()},
                validation=bool(flags.get("validation")), co_location=bool(flags.get("co_location")),
                context=p.get("context", "free")))
        except (KeyError, TypeError, ValueError) as e:
            print(f"  [calib] {rid}/{tag}: left out - {type(e).__name__}: {e}", file=sys.stderr)
    return out


def calib_events(house, calib_dir=None, also=None):
    """The rounds' events: calib_dir's (default_dir()), then the rounds of `also` whose ids calib_dir lacks - a fixed
    survey's curated copies first, the installation's newer captures after."""
    calib_dir = calib_dir or default_dir()
    rounds = load_rounds(calib_dir) if os.path.isdir(calib_dir) else []
    if also and os.path.isdir(also) and os.path.abspath(also) != os.path.abspath(calib_dir):
        have = {r["id"] for r in rounds}
        rounds += [r for r in load_rounds(also) if r["id"] not in have]
    ev = []
    for r in rounds:
        ev.extend(round_events(r, house))
    return ev


def trainable(e):
    """Validation rounds are scored only; co-location rounds measure tag offsets, not positions."""
    return not e.get("validation") and not e.get("co_location")
