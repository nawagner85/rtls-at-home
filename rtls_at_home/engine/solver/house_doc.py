"""The house document (spec docs/specs/2026-09-30-map-builder-design.md, section 1).

One JSON file in metres describes the house for the model, the engine and the viewer: floors (any number, bottom
first), rooms with a kind, open (wall-less) room edges, apertures on walls, generic objects (name, footprint, height,
material, construction) and stairs linking adjacent floors. Receivers and anchors are reserved for a later sub-project.
"""
import hashlib
import json
import math
import os
import tempfile

SCHEMA = 1
RES = 0.05                     # the engine's grid (geom3d.RES, the viewer): a document cannot choose another
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_HOUSE = os.path.normpath(os.path.join(HERE, "..", "house", "house.json"))
# A new household's first house (spec 2026-10-01-installable-and-published, A3): one floor, nothing on it - what the
# map editor opens when there is no house anywhere; Apply refuses it until it has rooms (house_check "rooms").
STARTING = {"schema": 1, "name": "My home", "units": "m", "display_units": "m",
            "frame": {"x0": 0.0, "y0": 0.0, "res": 0.05},
            "floors": [{"id": "floor1", "name": "Floor 1", "elevation": 0.0, "ceiling": 2.4, "slab": 0.3, "rooms": [],
                        "open_edges": [], "apertures": [], "objects": []}],
            "stairs": [], "receivers": [], "anchors": []}


def starting_doc():
    return json.loads(json.dumps(STARTING))


def repo_house():
    """The house the code ships with (Nick's dev repo; none in the published tree). RTLS_REPO_HOUSE overrides."""
    return os.environ.get("RTLS_REPO_HOUSE") or REPO_HOUSE

ROOM_KINDS = ("room", "closet", "stairs", "outdoor")
APERTURE_KINDS = ("window", "door", "garage_door", "opening")
MATERIALS = ("light", "wood", "stone", "metal", "glass", "water", "drywall", "none")
CONSTRUCTIONS = ("solid", "top", "panel")
DOOR_MATERIALS = ("wood", "metal", "glass")
APERTURE_DEFAULTS = {"window": (0.914, 2.032), "door": (0.0, 2.032), "garage_door": (0.0, 2.13), "opening": (0.0, None)}

# How much an object blocks the signal, by what it is made of and how it is built (spec section 1, Objects). The
# model's classes are densities: low (sofas, desks, toilets), med (cabinets, beds), high (cars, appliances).
_SOLID = {"light": "low", "glass": "low", "wood": "med", "stone": "high", "metal": "high", "drywall": "wall",
          "water": "water", "none": "none"}
_TOP = {"wood": "wood", "stone": "stone", "metal": "metal_top"}
_PANEL = {"metal": "metal_top", "wood": "door", "drywall": "wall"}


class HouseError(ValueError):
    def __init__(self, problems):
        super().__init__("house document: " + "; ".join(problems))
        self.problems = list(problems)


def object_class(material, construction):
    if material == "none":
        return "none"
    if construction == "top" and material in _TOP:
        return _TOP[material]
    if construction == "panel" and material in _PANEL:
        return _PANEL[material]
    return _SOLID[material]


def aperture_material(ap):
    """The model's aperture material: opening (no wall), glass, door (wood) or metal_door."""
    k = ap["kind"]
    if k == "opening":
        return "opening"
    if k == "window":
        return "glass"
    if k == "garage_door":
        return "metal_door"
    return {"wood": "door", "glass": "glass", "metal": "metal_door"}[ap.get("material", "wood")]


def aperture_band(ap, ceiling):
    """(sill, head) in m above the floor; a head of None reaches the ceiling."""
    d_sill, d_head = APERTURE_DEFAULTS[ap["kind"]]
    sill = float(ap["sill"]) if ap.get("sill") is not None else d_sill
    head = ap["head"] if "head" in ap else d_head
    return sill, float(ceiling) if head is None else float(head)


def _pt(p):
    return isinstance(p, (list, tuple)) and len(p) == 2 and all(_num(v) for v in p)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _floor_problems(f, out):
    fid = f.get("id")
    for k in ("elevation", "ceiling", "slab"):
        if not _num(f.get(k)):
            out.append(f"floor {fid}: {k} must be a number")
    if _num(f.get("ceiling")) and f["ceiling"] <= 0:
        out.append(f"floor {fid}: ceiling must be above 0")
    rids = [r.get("id") for r in f.get("rooms") or []]
    if len(set(rids)) != len(rids):
        out.append(f"floor {fid}: duplicate room id")
    for r in f.get("rooms") or []:
        if r.get("kind") not in ROOM_KINDS:
            out.append(f"room {r.get('id')}: kind {r.get('kind')!r} is not one of {', '.join(ROOM_KINDS)}")
        ol = r.get("outline") or []
        if len(ol) < 3 or not all(_pt(p) for p in ol):
            out.append(f"room {r.get('id')}: outline needs at least 3 [x, y] corners")
        if "step" in r and not _num(r["step"]):
            out.append(f"room {r.get('id')}: step must be a number")
        if not r.get("name"):
            out.append(f"room {r.get('id')}: no name")
    for e in f.get("open_edges") or []:
        if not (_pt(e.get("a")) and _pt(e.get("b"))):
            out.append(f"floor {fid}: an open edge needs a and b")
        if e.get("rooms") is not None and not (isinstance(e["rooms"], list) and 1 <= len(e["rooms"]) <= 2
                                               and all(r in rids for r in e["rooms"])):
            out.append(f"floor {fid}: an open edge's rooms must be one or two rooms of this floor")
    for a in f.get("apertures") or []:
        if a.get("kind") not in APERTURE_KINDS:
            out.append(f"aperture {a.get('id')}: kind {a.get('kind')!r} is not one of {', '.join(APERTURE_KINDS)}")
            continue
        if not (_pt(a.get("a")) and _pt(a.get("b"))):
            out.append(f"aperture {a.get('id')}: needs endpoints a and b")
        if a["kind"] == "door" and a.get("material", "wood") not in DOOR_MATERIALS:
            out.append(f"aperture {a.get('id')}: door material must be one of {', '.join(DOOR_MATERIALS)}")
            continue
        if _num(f.get("ceiling")):
            sill, head = aperture_band(a, f["ceiling"])
            if sill < 0 or head <= sill:
                out.append(f"aperture {a.get('id')}: head must be above the sill ({sill} .. {head})")
            elif head > f["ceiling"] + 1e-6:
                out.append(f"aperture {a.get('id')}: its top ({head} m) is above the ceiling ({f['ceiling']} m)")
    steps = {r.get("id"): r["step"] if _num(r.get("step")) else 0.0 for r in f.get("rooms") or []}
    for o in f.get("objects") or []:
        _object_problems(o, f, steps, out)


def _object_problems(o, f, steps, out):
    rids = set(steps)
    oid = o.get("id")
    if o.get("room") not in rids:
        out.append(f"object {oid}: room {o.get('room')!r} is not on floor {f.get('id')}")
    ol = o.get("outline") or []
    if len(ol) < 3 or not all(_pt(p) for p in ol):
        out.append(f"object {oid}: outline needs at least 3 [x, y] corners")
    if o.get("material") not in MATERIALS:
        out.append(f"object {oid}: material {o.get('material')!r} is not one of {', '.join(MATERIALS)}")
        return
    if o.get("construction") not in CONSTRUCTIONS:
        out.append(f"object {oid}: construction {o.get('construction')!r} is not one of {', '.join(CONSTRUCTIONS)}")
        return
    z0, h = o.get("z_min"), o.get("height")
    if not (_num(z0) and _num(h)):
        out.append(f"object {oid}: z_min and height must be numbers")
        return
    if object_class(o["material"], o["construction"]) != "none" and _num(f.get("ceiling")):
        if z0 < 0:
            out.append(f"object {oid}: z_min {z0} is below the floor")
        else:
            st = steps.get(o.get("room"), 0.0)          # the builder lifts an object by its room's step
            if not (z0 < z0 + h <= f["ceiling"] - st + 1e-6):
                raised = f" in a room raised {st} m" if st > 0 else ""
                out.append(f"object {oid}: {z0} .. {round(z0 + h, 4)} m{raised} must be a range below the ceiling "
                           f"({f['ceiling']} m)")
    pl = o.get("plate")
    if pl is not None and not (pl.get("material") in ("wood", "stone", "metal") and _num(pl.get("thickness"))):
        out.append(f"object {oid}: plate needs a wood / stone / metal material and a thickness")


def _stairs_problems(s, floors, fids, out):
    sid = s.get("id")
    lo, up = s.get("lower") or {}, s.get("upper") or {}
    ok = True
    for end in (lo, up):
        f = floors.get(end.get("floor"))
        if f is None or end.get("room") not in {r.get("id") for r in f.get("rooms") or []}:
            out.append(f"stairs {sid}: room {end.get('room')!r} on floor {end.get('floor')!r} does not exist")
            ok = False
    if ok and fids.index(up["floor"]) != fids.index(lo["floor"]) + 1:
        out.append(f"stairs {sid}: the upper end must be on the next floor up")
    run = s.get("run") or {}
    if run.get("axis") not in ("x", "y") or not (_num(run.get("from")) and _num(run.get("to"))) \
            or run.get("from") == run.get("to"):
        out.append(f"stairs {sid}: run needs axis x or y and distinct from / to")
    rs = s.get("risers")
    if rs is not None and not (isinstance(rs, list) and len(rs) == 3 and all(isinstance(v, int) and v >= 0 for v in rs)
                               and sum(rs) > 0):
        out.append(f"stairs {sid}: risers must be [flat, flight, flat] counts")
    if not (_pt(s.get("foot")) and _pt(s.get("top"))):
        out.append(f"stairs {sid}: foot and top must be [x, y]")


def problems(doc):
    """Everything wrong with a document, as sentences naming the thing; [] when it is usable."""
    out = []
    if doc.get("schema") != SCHEMA:
        out.append(f"schema {doc.get('schema')!r} is not {SCHEMA}")
    fr = doc.get("frame") or {}
    if not (_num(fr.get("x0", 0.0)) and _num(fr.get("y0", 0.0))):
        out.append("frame needs numeric x0, y0")
    if fr.get("res") != RES:
        out.append(f"frame res must be {RES} (the engine's grid), not {fr.get('res')!r}")
    floors = doc.get("floors") or []
    if not floors:
        out.append("no floors")
    fids = [f.get("id") for f in floors]
    if len(set(fids)) != len(fids):
        out.append("duplicate floor id")
    prev = None
    for f in floors:
        _floor_problems(f, out)
        if prev is not None and _num(f.get("elevation")) and _num(prev.get("elevation")) \
                and f["elevation"] <= prev["elevation"]:
            out.append(f"floor {f.get('id')}: elevation must be above floor {prev.get('id')}'s")
        prev = f
    by_id = {f.get("id"): f for f in floors}
    for s in doc.get("stairs") or []:
        _stairs_problems(s, by_id, fids, out)
    if not isinstance(doc.get("anchors", []), list):
        out.append("anchors must be a list")
    import house_receivers as HR            # the receivers' own checks (spec 2026-10-01)
    out += HR.problems(doc)
    return out


def default_path():
    """RTLS_HOUSE, else <RTLS_SESSIONS>/house.json when it exists, else the repo's house/house.json (repo_house)."""
    p = os.environ.get("RTLS_HOUSE")
    if p:
        return p
    s = os.environ.get("RTLS_SESSIONS")
    if s and os.path.exists(os.path.join(s, "house.json")):
        return os.path.join(s, "house.json")
    return repo_house()


def load(path=None):
    """The house at `path`, else the default one; with no house anywhere (a new household), the starting house."""
    if path is None and not os.path.exists(default_path()):
        return starting_doc()
    with open(path or default_path(), encoding="utf-8") as f:
        doc = json.load(f)
    probs = problems(doc)
    if probs:
        raise HouseError(probs)
    return doc


def save(doc, path):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def doc_hash(doc):
    return hashlib.sha1(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def floor_index(doc, fid):
    for i, f in enumerate(doc["floors"]):
        if f["id"] == fid:
            return i
    raise KeyError(fid)
