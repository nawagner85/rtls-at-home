"""Outlet survey (Nick 2026-09-25): every available outlet on every floor, marked in the viewer's Outlets mode, for the
XIAO ESP32S3 proxy placement. Saved to <sessions>/outlets.json; the first start copies solver/outlet_candidates.json
(the outlets measured before the survey) so nothing is lost. tools/fetch_outlets.py writes the survey back to that
file for the placement optimisation.

Heights (Nick): standard outlets 16 in, counter outlets 44 in. The XIAO's antenna starts ~3.5 in above the outlet.
"""
import json
import math
import os
import threading
import time

from calib import _floor_bounds, write_json_atomic

IN = 0.0254
HEIGHTS = {"standard": 16 * IN, "counter": 44 * IN}
ANTENNA_OFFSET_M = 3.5 * IN
_IDS = []


def _floor_ids():
    """The house's floor ids, bottom first (house.json), loaded once."""
    if not _IDS:
        import house_doc as HD
        _IDS.extend(f["id"] for f in HD.load()["floors"])
    return _IDS


def _floor_index(fl):
    return _floor_ids().index(fl) if fl in _floor_ids() else fl
OLD_DEFAULT_Z = 0.30          # the candidate file's "0.30 m unless measured": an assumed standard outlet
MAX_NOTE, MAX_STATUS = 200, 80
EDITABLE = {"x", "y", "floor", "height", "note", "status"}


class OutletError(ValueError):
    pass


class NoSuchOutlet(KeyError):
    pass


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _from_seed(o):
    """A candidate-file entry as a store entry: floor name -> index, height class from z_rel, notes -> note."""
    out = {k: v for k, v in o.items() if k not in ("notes", "z_abs")}
    fl = o.get("floor")
    out["floor"] = _floor_index(fl)
    z = o.get("z_rel")
    if o.get("height") in HEIGHTS:                    # an exported survey (to_candidate_file) says so itself
        out["z_rel"] = HEIGHTS[o["height"]]
    elif o.get("height") == "measured" and z is not None:
        pass
    elif z is None or abs(z - OLD_DEFAULT_Z) < 1e-6:
        out["height"], out["z_rel"] = "standard", HEIGHTS["standard"]
    elif abs(z - HEIGHTS["counter"]) < 0.01:
        out["height"], out["z_rel"] = "counter", HEIGHTS["counter"]
    else:
        out["height"] = "measured"
    out["note"] = o.get("notes") or ""
    out.setdefault("status", "candidate")
    return out


class OutletStore:
    def __init__(self, path, house, seed=None, clock=time.time, log=print):
        """seed: path of solver/outlet_candidates.json, copied when outlets.json is missing or unreadable."""
        self.path, self.house, self.seed, self.clock, self.log = path, house, seed, clock, log
        self.lock = threading.Lock()
        self.recovered = None
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict) or not isinstance(d.get("outlets"), list):
                raise ValueError("not an outlet list")
            self.outlets, self.next_id = list(d["outlets"]), int(d.get("next_id", 1))
        except FileNotFoundError:
            self._reseed()
        except (OSError, ValueError, TypeError) as e:
            os.replace(path, path + ".bak")
            self.recovered = dict(backup=path + ".bak", error=f"{type(e).__name__}: {e}")
            self.log("outlets.json unreadable, set aside:", self.recovered["error"])
            self._reseed()

    def _reseed(self):
        rows = []
        if self.seed and os.path.exists(self.seed):
            with open(self.seed, encoding="utf-8") as f:
                rows = json.load(f).get("outlets") or []
        self.outlets, self.next_id = [self._place(_from_seed(o)) for o in rows], 1
        self.save()

    def save(self):
        write_json_atomic(self.path, dict(version=1, next_id=self.next_id, outlets=self.outlets))

    def view(self):
        with self.lock:
            return dict(outlets=[dict(o) for o in self.outlets], heights=dict(HEIGHTS), antenna_offset_m=ANTENNA_OFFSET_M,
                        recovered=self.recovered)

    # ---- editing: every call saves ----------------------------------------------------
    def _place(self, o):
        """Fill room and z_abs from floor, x, y and z_rel (an unplaced outlet keeps room from its source)."""
        if o.get("x") is not None and o.get("y") is not None and o.get("floor") is not None:
            o["room"] = self.house.room_at(int(o["floor"]), float(o["x"]), float(o["y"]))
        if o.get("floor") is not None and o.get("z_rel") is not None:
            o["z_abs"] = round(self.house.floor_z(int(o["floor"]), o.get("room")) + float(o["z_rel"]), 3)
        return o

    def _check(self, o):
        fi = o.get("floor")
        if not isinstance(fi, int) or isinstance(fi, bool) or not 0 <= fi < len(self.house.floors):
            raise OutletError(f"no floor {fi!r}")
        try:
            x, y = float(o["x"]), float(o["y"])
        except (KeyError, TypeError, ValueError):
            raise OutletError("x and y must be numbers (metres)")
        x0, x1, y0, y1 = _floor_bounds(self.house, fi)
        if not (math.isfinite(x) and math.isfinite(y) and x0 <= x <= x1 and y0 <= y <= y1):
            raise OutletError(f"({x:.2f}, {y:.2f}) is outside the house")
        o["x"], o["y"] = round(x, 3), round(y, 3)
        if o.get("height") != "measured":
            if o.get("height") not in HEIGHTS:
                raise OutletError(f"height must be one of: {', '.join(HEIGHTS)}")
            o["z_rel"] = HEIGHTS[o["height"]]
        if len(str(o.get("note") or "")) > MAX_NOTE:
            raise OutletError(f"note: at most {MAX_NOTE} characters")
        if len(str(o.get("status") or "")) > MAX_STATUS:
            raise OutletError(f"status: at most {MAX_STATUS} characters")
        return self._place(o)

    def _get(self, oid):
        for o in self.outlets:
            if o["id"] == oid:
                return o
        raise NoSuchOutlet(oid)

    @staticmethod
    def _preset(body):
        """The API sets only the two presets; "measured" heights come from the candidate file alone."""
        if "height" in body and body["height"] not in HEIGHTS:
            raise OutletError(f"height must be one of: {', '.join(HEIGHTS)}")

    def add(self, body):
        self._preset(body)
        o = dict(floor=body.get("floor"), x=body.get("x"), y=body.get("y"), height=body.get("height", "standard"),
                 note=str(body.get("note") or ""), status="candidate", added=_iso(self.clock()))
        with self.lock:
            o = self._check(o)
            o["id"] = f"o{self.next_id}"
            self.next_id += 1
            self.outlets.append(o)
            self.save()
            return dict(o)

    def update(self, oid, body):
        unknown = set(body) - EDITABLE
        if unknown:
            raise OutletError(f"unknown field(s): {', '.join(sorted(unknown))}")
        if ("x" in body) != ("y" in body):
            raise OutletError("x and y go together")
        self._preset(body)
        with self.lock:
            old = self._get(oid)
            new = self._check(dict(old, **body))
            self.outlets[self.outlets.index(old)] = new
            self.save()
            return dict(new)

    def remove(self, oid):
        with self.lock:
            self.outlets.remove(self._get(oid))
            self.save()
        return dict(id=oid, removed=True)


def to_candidate_file(view):
    """The survey as solver/outlet_candidates.json (tools/fetch_outlets.py): floor names, "notes", and each outlet's
    antenna height (the XIAO's antenna starts ANTENNA_OFFSET_M above the outlet) for the placement optimisation."""
    off = view["antenna_offset_m"]
    outs = []
    for o in view["outlets"]:
        c = {k: v for k, v in o.items() if k != "note"}
        fl = o.get("floor")
        c["floor"] = _floor_ids()[fl] if isinstance(fl, int) and 0 <= fl < len(_floor_ids()) else fl
        c["notes"] = o.get("note") or ""
        if o.get("z_abs") is not None:
            c["antenna_z_abs"] = round(o["z_abs"] + off, 3)
        outs.append(c)
    return {"_frame": "same as scanners_v2.json: the house.json frame, x east, y north, metres; z_abs = slab + z_rel "
                      "(the outlet); antenna_z_abs = z_abs + antenna_offset_m (where the XIAO's antenna starts)",
            "_purpose": "Every available outlet, from the viewer's Outlets mode (outlet survey, Nick 2026-09-25), for the "
                        "XIAO ESP32S3 proxy placement. Written by tools/fetch_outlets.py - edit outlets in the app.",
            "antenna_offset_m": off, "heights": dict(view["heights"]), "outlets": outs}
