"""Build the model's maps from the house document (spec docs/specs/2026-09-30-map-builder-design.md, section 2).

Per floor, the files geom3d reads - <id>.floorplan.json, .rooms.png, .rf.png, .layers.npz, .apertures.npz - on the
frame's 5 cm grid. The painting is floormap_v3/build_floorplan.py's, from metres instead of vacuum-screenshot pixels,
so Nick's converted house rebuilds cell for cell (tests/test_house_parity.py): rooms, then objects (later ones
overwrite), wall lines 3 cells wide, then apertures pulled back 3 cells from each end.

Walls are derived as build_floorplan.py derived them: every room edge is sampled every STEP px of a TRACE-metre grid
(its 0.0128 m screenshot pixel) and a probe OFF px outward names the neighbour. Kind: `opening` on one of the floor's
open edges or where an outdoor room meets nothing, `exterior` where there is no neighbour or indoor meets outdoor,
`wall` otherwise. Runs of one (neighbour, kind) become segments, runs under MIN_RUN px are dropped, and a shared edge
is emitted once, from the earlier room.
"""
import hashlib
import json
import os
import shutil
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import house_doc as HD                       # noqa: E402
from nudge import nudge_off_walls            # noqa: E402

RF_CODE = {"none": 0, "low": 1, "med": 2, "high": 3, "wall": 4, "door": 7, "water": 9,
           "wood": 10, "stone": 11, "metal_top": 12}
AP_CODE = {"opening": 0, "glass": 6, "door": 7, "metal_door": 8}
OLD_KIND = {"window": "window", "door": "door", "garage_door": "door", "opening": "opening"}
LAYERS = 4
TRACE, STEP, OFF, MIN_RUN = 0.0128, 3, 6, 4
NO_NUDGE = ("none", "wall", "door")          # walls, doors and nothing stay where they are drawn
HERE = os.path.dirname(os.path.abspath(__file__))


class BuildError(ValueError):
    pass


def _tp(p):
    """Metres -> trace px (x right, y down; an integer lattice; origin arbitrary)."""
    return (int(round(p[0] / TRACE)), -int(round(p[1] / TRACE)))


def _m(p):
    """Trace px -> metres, rounded as build_floorplan.py rounded."""
    return [round(p[0] * TRACE, 3) + 0.0, round(-p[1] * TRACE, 3) + 0.0]       # + 0.0: no -0.0


def _on_open(px, py, opens, ex, ey, pair):
    """Is this sample of an edge running along (ex, ey) between the rooms `pair` on one of the floor's open edges?
    Only an open edge parallel to the sampled edge counts (one ending on a corner must not open the wall running
    across its end), and an open edge that names its rooms opens only the wall between them (one ending where
    another wall starts on the same line must not open that wall)."""
    el = (ex * ex + ey * ey) ** 0.5
    for (ax, ay), (bx, by), rooms in opens:
        if rooms is not None and rooms != pair:
            continue
        dx, dy = bx - ax, by - ay
        L = (dx * dx + dy * dy) ** 0.5
        if L == 0 or abs(ex * dy - ey * dx) / (L * el) > 0.01:
            continue
        s = ((px - ax) * dx + (py - ay) * dy) / L
        if -0.1 <= s <= L + 0.1 and abs((px - ax) * dy - (py - ay) * dx) / L <= 0.25:
            return True
    return False


def derive_walls(fl):
    """[dict(kind, rooms, a, b, length_m)] for one floor of the document, in metres (see the module docstring)."""
    rooms = fl["rooms"]
    P = [[_tp(p) for p in r["outline"]] for r in rooms]
    xs = [p[0] for Q in P for p in Q]
    ys = [p[1] for Q in P for p in Q]
    pad = OFF + 2
    ox, oy = pad - min(xs), pad - min(ys)
    W, H = max(xs) + ox + pad + 1, max(ys) + oy + pad + 1
    rl = np.zeros((H, W), np.int32)
    for i, Q in enumerate(P, 1):
        cv2.fillPoly(rl, [np.array([(x + ox, y + oy) for x, y in Q], np.int32)], i)

    def room_at(x, y):
        x, y = int(round(x)) + ox, int(round(y)) + oy
        if 0 <= x < W and 0 <= y < H and rl[y, x]:
            return int(rl[y, x]) - 1
        return None

    opens = [(_tp(e["a"]), _tp(e["b"]), frozenset(e["rooms"]) if e.get("rooms") else None)   # on the trace lattice
             for e in fl.get("open_edges") or []]
    outdoor = [r["kind"] == "outdoor" for r in rooms]
    raw = []
    for ri, Q in enumerate(P):
        area2 = sum(Q[i][0] * Q[(i + 1) % len(Q)][1] - Q[(i + 1) % len(Q)][0] * Q[i][1] for i in range(len(Q)))
        sign = 1 if area2 > 0 else -1
        for i in range(len(Q)):
            (x0, y0), (x1, y1) = Q[i], Q[(i + 1) % len(Q)]
            L = max(abs(x1 - x0), abs(y1 - y0))
            if L == 0:
                continue
            dx, dy = (x1 - x0) / L, (y1 - y0) / L
            nx, ny = dy * sign, -dx * sign
            run = None
            for t in list(range(0, L, STEP)) + [L]:
                px, py = x0 + dx * t, y0 + dy * t
                nb = room_at(px + nx * OFF, py + ny * OFF)
                pair = frozenset([rooms[ri]["id"]] + ([rooms[nb]["id"]] if nb is not None else []))
                if _on_open(px, py, opens, dx, dy, pair):
                    kind = "opening"
                elif nb is None:
                    kind = "opening" if outdoor[ri] else "exterior"
                elif outdoor[nb] != outdoor[ri]:
                    kind = "exterior"
                else:
                    kind = "wall"
                key = (nb, kind)
                if run and run[0] == key:
                    run[2] = (px, py)
                else:
                    if run:
                        raw.append((ri, *run[0], run[1], run[2]))
                    run = [key, (px, py), (px, py)]
            raw.append((ri, *run[0], run[1], run[2]))
    walls = []
    for ri, nb, kind, p0, p1 in raw:
        if abs(p0[0] - p1[0]) + abs(p0[1] - p1[1]) < MIN_RUN:
            continue
        if nb is not None and ri > nb:
            continue
        walls.append(dict(kind=kind, rooms=[rooms[ri]["id"]] + ([rooms[nb]["id"]] if nb is not None else []),
                          a=_m(p0), b=_m(p1),
                          length_m=round(TRACE * (abs(p0[0] - p1[0]) + abs(p0[1] - p1[1])), 2)))
    return walls


def _poly_stats(pts_m):
    q = np.array([_tp(p) for p in pts_m], np.float32)
    M = cv2.moments(q)
    if not M["m00"]:
        return 0.0, list(map(float, pts_m[0]))
    return round(cv2.contourArea(q) * TRACE * TRACE, 2), _m((M["m10"] / M["m00"], M["m01"] / M["m00"]))


def build_floor(doc, fi):
    F = doc["floors"][fi]
    fr = doc["frame"]
    g, x0, y0 = float(fr["res"]), float(fr.get("x0", 0.0)), float(fr.get("y0", 0.0))
    rooms = F["rooms"]
    walls = derive_walls(F)
    xmax = round(max(p[0] for r in rooms for p in r["outline"]), 3)
    ymax = round(max(p[1] for r in rooms for p in r["outline"]), 3)
    W_m, D_m = round(xmax - x0, 3), round(ymax - y0, 3)
    gw, gh = int(np.ceil(W_m / g)) + 1, int(np.ceil(D_m / g)) + 1

    def gp(P):
        return np.array([[round((p[0] - x0) / g), round((ymax - p[1]) / g)] for p in P], np.int32)

    step = {r["id"]: float(r.get("step", 0.0)) for r in rooms}
    rooms_out = []
    for r in rooms:
        area, cen = _poly_stats(r["outline"])
        rooms_out.append(dict(id=r["id"], name=r["name"], kind=r["kind"], outdoor=r["kind"] == "outdoor",
                              area_m2=area, centroid=cen, points=[list(p) for p in r["outline"]],
                              floor_m=step[r["id"]], ha_area=r.get("ha_area")))
    fx = []
    for o in F.get("objects") or []:
        cls = HD.object_class(o["material"], o["construction"])
        z0 = float(o["z_min"])
        z1 = round(z0 + float(o["height"]), 6)
        off = step.get(o["room"], 0.0)
        if off and z1:
            z0, z1 = max(0.0, round(z0 + off, 4)), round(z1 + off, 4)
        area, cen = _poly_stats(o["outline"])
        fx.append(dict(id=o["id"], name=o.get("name", o["id"]), room=o["room"], rf=cls, height_m=z1, z_min=z0,
                       material=o["material"], construction=o["construction"], note=o.get("note", ""),
                       centroid=cen, points=[list(p) for p in o["outline"]]))
    rooms_r = np.zeros((gh, gw), np.uint8)
    rf_r = np.full((gh, gw), 255, np.uint8)
    for i, r in enumerate(rooms_out, 1):
        cv2.fillPoly(rooms_r, [gp(r["points"])], i)
        cv2.fillPoly(rf_r, [gp(r["points"])], 0)
    empty = [r["id"] for i, r in enumerate(rooms_out, 1) if not (rooms_r == i).any()]
    if empty:            # rooms paint in order: a later room covering this one entirely, or one smaller than a cell
        raise BuildError(f"floor {F['id']}: no cells of their own for room(s) {', '.join(empty)} "
                         "(covered by a later room, or smaller than a cell)")
    wall_m = np.zeros((gh, gw), np.uint8)
    for w in walls:
        if w["kind"] != "opening":
            cv2.line(wall_m, tuple(int(v) for v in gp([w["a"]])[0]), tuple(int(v) for v in gp([w["b"]])[0]), 1, 2)
    for f in fx:
        if f["rf"] in NO_NUDGE:
            continue
        cells = [((p[0] - x0) / g, (ymax - p[1]) / g) for p in f["points"]]
        new, moved = nudge_off_walls(cells, wall_m.astype(bool))
        if moved:
            f["points"] = [[round(cx * g + x0, 4), round(ymax - cy * g, 4)] for cx, cy in new]
            f["nudged_cells"] = moved
    cls_l = np.zeros((LAYERS, gh, gw), np.uint8)
    zmin_l = np.zeros((LAYERS, gh, gw), np.float32)
    zmax_l = np.zeros((LAYERS, gh, gw), np.float32)
    names_at = {}
    for f in fx:
        code = RF_CODE[f["rf"]]
        if not code:
            continue
        cv2.fillPoly(rf_r, [gp(f["points"])], code)
        mask = np.zeros((gh, gw), np.uint8)
        cv2.fillPoly(mask, [gp(f["points"])], 1)
        for r, c in zip(*mask.nonzero()):
            k = int((cls_l[:, r, c] != 0).sum())
            if k >= LAYERS:
                raise BuildError(f"floor {F['id']}: more than {LAYERS} objects on one spot: "
                                 + ", ".join(names_at[(r, c)] + [f["id"]]))
            cls_l[k, r, c], zmin_l[k, r, c], zmax_l[k, r, c] = code, f["z_min"], f["height_m"]
            names_at.setdefault((r, c), []).append(f["id"])
    for w in walls:
        if w["kind"] != "opening":
            cv2.line(rf_r, tuple(int(v) for v in gp([w["a"]])[0]), tuple(int(v) for v in gp([w["b"]])[0]),
                     5 if w["kind"] == "exterior" else 4, 2)
    ap_out = []
    ap_code = np.zeros((gh, gw), np.uint8)
    ap_sill = np.zeros((gh, gw), np.float32)
    ap_head = np.zeros((gh, gw), np.float32)
    for a in F.get("apertures") or []:
        A, B = [float(v) for v in a["a"]], [float(v) for v in a["b"]]
        mat = HD.aperture_material(a)
        sill, head = HD.aperture_band(a, F["ceiling"])
        ap_out.append(dict(id=a["id"], kind=OLD_KIND[a["kind"]], material=mat, a=A, b=B, sill_m=sill, head_m=head,
                           length_m=round(float(np.hypot(B[0] - A[0], B[1] - A[1])), 2), note=a.get("note", "")))
        pa, pb = np.array(gp([A])[0], float), np.array(gp([B])[0], float)
        L = float(np.hypot(*(pb - pa)))
        if L > 8:
            u = (pb - pa) / L
            pa, pb = pa + 3 * u, pb - 3 * u
        p0, p1 = tuple(int(v) for v in np.round(pa)), tuple(int(v) for v in np.round(pb))
        cv2.line(rf_r, p0, p1, AP_CODE[mat], 2)
        cv2.line(ap_code, p0, p1, AP_CODE[mat], 2)
        cv2.line(ap_sill, p0, p1, sill, 2)
        cv2.line(ap_head, p0, p1, head, 2)
    floorplan = dict(
        floor=dict(id=F["id"], name=F["name"], z=float(F["elevation"]), ceiling=float(F["ceiling"]),
                   slab=float(F["slab"]), ha_floor=F.get("ha_floor"),
                   bounds=[[x0, y0, float(F["elevation"])], [xmax, ymax, round(F["elevation"] + F["ceiling"], 2)]]),
        units="m", frame=dict(x0=x0, y0=y0, res=g),
        rooms=rooms_out, fixtures=fx, walls=walls, apertures=ap_out, nodes=[],
        raster_index={str(i): r["id"] for i, r in enumerate(rooms_out, 1)})
    return dict(rooms=rooms_r, rf=rf_r, cls=cls_l, zmin=zmin_l, zmax=zmax_l, ap_code=ap_code, ap_sill=ap_sill,
                ap_head=ap_head, floorplan=floorplan)


def build(doc, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    for fi, F in enumerate(doc["floors"]):
        b = build_floor(doc, fi)
        base = os.path.join(out_dir, F["id"])
        cv2.imwrite(base + ".rooms.png", b["rooms"])
        cv2.imwrite(base + ".rf.png", b["rf"])
        np.savez(base + ".layers.npz", cls=b["cls"], zmin=b["zmin"], zmax=b["zmax"])
        np.savez(base + ".apertures.npz", code=b["ap_code"], sill=b["ap_sill"], head=b["ap_head"])
        with open(base + ".floorplan.json", "w", encoding="utf-8") as f:
            json.dump(b["floorplan"], f, indent=1)
    with open(os.path.join(out_dir, "BUILD_OK"), "w") as f:
        f.write(HD.doc_hash(doc) + "\n")
    return out_dir


BAKED_ROOT = os.path.normpath(os.path.join(HERE, "..", "house", "build"))   # the image's builds: read-only fallback


def build_root():
    """Where builds go: RP_BUILD_DIR, else <RTLS_SESSIONS>/build (the data folder, so an applied house's build survives
    the container being replaced - spec 2026-09-30 section 5), else the repo's house/build."""
    if os.environ.get("RP_BUILD_DIR"):
        return os.environ["RP_BUILD_DIR"]
    s = os.environ.get("RTLS_SESSIONS")
    return os.path.join(s, "build") if s else BAKED_ROOT


def code_hash():
    """The builder's own code (this file, the nudge, the document's class table): a change to any of them is a new
    build folder, never a stale one reused (final review 2026-09-30)."""
    h = hashlib.sha1()
    for fn in ("house_build.py", "nudge.py", "house_doc.py"):
        with open(os.path.join(HERE, fn), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:8]


def build_name(doc):
    """The build folder's name: the document's maps (receivers and anchors left out) and this builder's code."""
    import house_receivers as HR
    return f"{HR.build_key(doc)[:12]}-{code_hash()}"


def ensure_build(doc, root=None):
    """The build folder for this document and this builder under root (built once, atomically)."""
    root = root or build_root()
    name = build_name(doc)
    out = os.path.join(root, name)
    if os.path.exists(os.path.join(out, "BUILD_OK")):
        return out
    baked = os.path.join(BAKED_ROOT, name)               # the same build, baked into the image at deploy
    if os.path.abspath(baked) != os.path.abspath(out) and os.path.exists(os.path.join(baked, "BUILD_OK")):
        return baked
    shutil.rmtree(out, ignore_errors=True)        # a folder without its marker is incomplete: build it again
    tmp = out + f".tmp{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    build(doc, tmp)
    try:
        os.replace(tmp, out)
    except OSError:                       # another process finished the same build first
        shutil.rmtree(tmp, ignore_errors=True)
    return out
