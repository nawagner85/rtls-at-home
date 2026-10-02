"""The map editor's problems list (spec docs/specs/2026-09-30-map-builder-design.md, section 4).

check(doc) -> [dict(code, severity, message, floor, ids)], each naming the floor and the elements it is about so the
editor can select them. Errors block Apply; warnings are shown and can be dismissed (spec: "fixed or dismissed").
  doc          error    house_doc.problems (the document itself is malformed)
  rooms        error    a floor with no rooms yet (a new household's starting house): nothing to build or locate in
  build        error    house_build.BuildError on a floor (too many objects stacked, a room with no cells of its own)
  overlap      error    two rooms of a floor share more than OVERLAP_CELLS cells of the 5 cm grid, each painted alone
  stairs_kind  error    a stairs record naming a room whose kind is not 'stairs'
  sliver       warning  two rooms just far enough apart that the build sees no neighbour across the gap, so both
                        sides become exterior walls (the double back wall of 2026-09-25). Rooms traced to the inside
                        faces of walls (a gap up to the builder's 7.7 cm probe) are one shared wall and fine.
  aperture     warning  a door / window / doorway more than ON_WALL_M from every room edge (Nick's shower glass is a
                        glass line in a room on purpose)
  object       warning  an object whose centroid is outside its room (its heights are from that room's floor; the
                        kitchen island's pony wall straddles the kitchen | living boundary on purpose)
"""
import math
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import house_build as HB          # noqa: E402
import house_doc as HD            # noqa: E402

OVERLAP_CELLS = 4
SLIVER_M = 0.10
ON_WALL_M = 0.05
FRAME_SLACK_M = 0.1         # the converted house's traced walls sit up to 5 cm past its 0, 0 origin (parity-verified)
TOUCH_M = 0.005


WARNINGS = ("sliver", "aperture", "object", "stairs_pair", "receiver_place")
RECEIVER_SLACK_M = 0.25      # a proxy on an outlet is recorded at the wall line, a hand's width outside its room


def _p(code, message, floor=None, ids=()):
    return dict(code=code, severity="warning" if code in WARNINGS else "error", message=message, floor=floor,
                ids=list(ids))


def _ok_outline(o):
    ol = o.get("outline") or []
    return len(ol) >= 3 and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in ol)


def _floor_ok(f):
    return all(_ok_outline(r) for r in f.get("rooms") or []) and all(_ok_outline(o) for o in f.get("objects") or [])


def _edges(outline):
    return [(outline[i], outline[(i + 1) % len(outline)]) for i in range(len(outline))]


def _seg_dist(p, a, b):
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _inside(p, pts):
    x, y = p
    hit = False
    for (xi, yi), (xj, yj) in zip(pts, pts[-1:] + pts[:-1]):
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
    return hit


def _centroid(pts):
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
        c = x0 * y1 - x1 * y0
        a += c
        cx += (x0 + x1) * c
        cy += (y0 + y1) * c
    if abs(a) < 1e-12:
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    return (cx / (3 * a), cy / (3 * a))


def _overlaps(doc, fi, f):
    fr = doc["frame"]
    g, x0 = float(fr["res"]), float(fr.get("x0", 0.0))
    rooms = f["rooms"]
    xmax = max(p[0] for r in rooms for p in r["outline"])
    ymax = max(p[1] for r in rooms for p in r["outline"])
    ymin = min(p[1] for r in rooms for p in r["outline"])
    gw, gh = int(np.ceil((xmax - x0) / g)) + 2, int(np.ceil((ymax - min(ymin, float(fr.get("y0", 0.0)))) / g)) + 2
    masks = []
    for r in rooms:
        m = np.zeros((gh, gw), np.uint8)
        cv2.fillPoly(m, [np.array([[round((p[0] - x0) / g), round((ymax - p[1]) / g)] for p in r["outline"]], np.int32)], 1)
        # a room's own boundary row/column is shared with its neighbour: count interiors only
        masks.append(cv2.erode(m, np.ones((3, 3), np.uint8)))
    out = []
    for i in range(len(rooms)):
        for j in range(i + 1, len(rooms)):
            n = int((masks[i] & masks[j]).sum())
            if n > OVERLAP_CELLS:
                out.append(_p("overlap", f"{rooms[i]['name']} and {rooms[j]['name']} overlap "
                                         f"({n * g * g:.2f} m²)", f["id"], (rooms[i]["id"], rooms[j]["id"])))
    return out


def _slivers(f):
    """Exterior wall runs (house_build.derive_walls) with another indoor room just across them."""
    rooms = f["rooms"]
    by_id = {r["id"]: r for r in rooms}
    probe = HB.OFF * HB.TRACE
    out, seen = [], set()
    for w in HB.derive_walls(f):
        if w["kind"] != "exterior" or len(w["rooms"]) != 1:
            continue
        a, b = w["a"], w["b"]
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        if L < SLIVER_M:
            continue
        mid, n = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), ((b[1] - a[1]) / L, (a[0] - b[0]) / L)
        own = by_id[w["rooms"][0]]
        side = 1 if not _inside((mid[0] + n[0] * 0.02, mid[1] + n[1] * 0.02), own["outline"]) else -1   # outward
        for k in range(int(round(probe * 1000)) + 1, int(round(SLIVER_M * 1000)) + 1, 5):
            q = (mid[0] + side * n[0] * k / 1000, mid[1] + side * n[1] * k / 1000)
            other = next((r for r in rooms if r is not own and r["kind"] != "outdoor" and _inside(q, r["outline"])), None)
            if other is not None:
                key = tuple(sorted((own["id"], other["id"])))
                if key not in seen:
                    seen.add(key)
                    out.append(_p("sliver", f"{own['name']} and {other['name']} are {k / 10:.0f} cm apart: the build "
                                            "makes both sides exterior walls - join them", f["id"], key))
                break
    return out


def _past_frame(doc, f):
    """Rooms reaching west or south of the rasters' origin by more than a traced wall: the build would cut them off
    (the editor moves the origin out as rooms are drawn; a document edited elsewhere may not have)."""
    fr = doc.get("frame") or {}
    x0, y0, out = float(fr.get("x0", 0.0)), float(fr.get("y0", 0.0)), []
    for r in f["rooms"]:
        xs, ys = [p[0] for p in r["outline"]], [p[1] for p in r["outline"]]
        dx, dy = x0 - min(xs), y0 - min(ys)
        if max(dx, dy) > FRAME_SLACK_M:
            out.append(_p("frame", f"{r.get('name', r.get('id'))} reaches {max(dx, dy):.2f} m past the map's origin, "
                                   "so the build would cut it off: move the origin out (frame x0 / y0)", f["id"], (r.get("id"),)))
    return out


def _receivers(doc):
    """Receivers (spec 2026-10-01): one name and one address each, inside (or on the wall line of) their room."""
    out, seen = [], {}
    floors = {f.get("id"): f for f in doc.get("floors") or []}
    for r in doc.get("receivers") or []:
        if not isinstance(r, dict):
            continue
        who = r.get("name") or r.get("id")
        for key in ("name", "mac", "address"):
            v = r.get(key)
            if isinstance(v, str) and v.strip():
                k = (key, v.strip() if key == "name" else v.strip().lower())
                if k in seen:
                    out.append(_p("receiver", f"{who} has the same {key} as {seen[k]}: each receiver needs its own",
                                  r.get("floor"), (r.get("id"),)))
                else:
                    seen[k] = who
        f = floors.get(r.get("floor"))
        if f is None or not all(isinstance(r.get(c), (int, float)) for c in ("x", "y")):
            continue                                       # house_doc already names these
        p = (r["x"], r["y"])
        room = next((x for x in f.get("rooms") or [] if x.get("id") == r.get("room")), None)
        if room is None or not (_inside(p, room["outline"]) or
                                min(_seg_dist(p, a, b) for a, b in _edges(room["outline"])) <= RECEIVER_SLACK_M):
            out.append(_p("receiver", f"{who} is not in {room['name'] if room else 'a room'} on "
                                      f"{f.get('name', f.get('id'))}: move it onto the map where it is", f.get("id"),
                          (r.get("id"),)))
        elif room.get("kind") in ("closet", "outdoor"):
            out.append(_p("receiver_place", f"{who} is in {room['name']}, a {room['kind']}: fine if that is where it is",
                          f.get("id"), (r.get("id"),)))
    return out


def _missing_refs(doc, refs):
    """Rooms the engine's receivers and calibration data stand in, by floor id and room name, that the document no
    longer has (renamed or removed): the engine cannot start without them."""
    have = {(f.get("id"), r.get("name")) for f in doc.get("floors") or [] for r in f.get("rooms") or []}
    out = []
    for (fid, name), who in sorted(refs.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        if (fid, name) in have:
            continue
        names = sorted(set(map(str, who)))
        shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
        out.append(_p("refs", f"{name} is renamed or gone, but the receivers and calibration data name it ({shown}): "
                              "keep that name for now", fid))
    return out


def check(doc, refs=None):
    probs = [_p("doc", m) for m in HD.problems(doc)]
    for fi, f in enumerate(doc.get("floors") or []):
        if not f.get("rooms"):
            probs.append(_p("rooms", f"{f.get('name') or f.get('id')} has no rooms yet: draw at least one before "
                                     "applying", f.get("id")))
            continue
        if not _floor_ok(f):
            continue
        try:
            HB.build_floor(doc, fi)
        except HB.BuildError as e:
            probs.append(_p("build", str(e), f.get("id")))
        except Exception:                               # a document house_doc already rejected: reported above
            if not probs:
                raise
            continue
        probs += _overlaps(doc, fi, f)
        probs += _past_frame(doc, f)
        probs += _slivers(f)
        edges = [e for r in f["rooms"] for e in _edges(r["outline"])]
        for a in f.get("apertures") or []:
            try:
                mid = ((a["a"][0] + a["b"][0]) / 2, (a["a"][1] + a["b"][1]) / 2)
            except (KeyError, TypeError, IndexError):
                continue
            if min(_seg_dist(mid, e0, e1) for e0, e1 in edges) > ON_WALL_M:
                probs.append(_p("aperture", f"{a.get('kind', 'aperture')} {a.get('id')} is not on a wall", f["id"],
                                (a.get("id"),)))
        rooms = {r["id"]: r for r in f["rooms"]}
        for o in f.get("objects") or []:
            r = rooms.get(o.get("room"))
            if r is not None and not _inside(_centroid(o["outline"]), r["outline"]):
                probs.append(_p("object", f"{o.get('name', o.get('id'))} is outside {r['name']}", f["id"], (o.get("id"),)))
    if refs:
        probs += _missing_refs(doc, refs)
    probs += _receivers(doc)
    floors = {f.get("id"): f for f in doc.get("floors") or []}
    pairs = set()
    for s in doc.get("stairs") or []:        # the model keeps one route per pair of floors (geom3d PORTALS)
        pair = ((s.get("lower") or {}).get("floor"), (s.get("upper") or {}).get("floor"))
        if pair in pairs:
            probs.append(_p("stairs_pair", f"stairs {s.get('id')}: a second staircase between {pair[0]} and {pair[1]}; "
                                           "the model routes between them through the first one only", pair[0],
                            (s.get("id"),)))
        pairs.add(pair)
    for s in doc.get("stairs") or []:
        for end in ("lower", "upper"):
            e = s.get(end) or {}
            r = next((r for r in (floors.get(e.get("floor")) or {}).get("rooms") or [] if r.get("id") == e.get("room")), None)
            if r is not None and r.get("kind") != "stairs":
                probs.append(_p("stairs_kind", f"stairs {s.get('id')}: {r['name']} is not a stairs room", e.get("floor"),
                                (r["id"],)))
    return probs
