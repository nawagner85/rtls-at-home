"""Pictures of the house for Home Assistant image entities (spec docs/specs/2026-10-02-house-renders-design.md),
the viewer's own axonometric look (decision 3).

The pure part first: the camera's orthographic projection, the raster's back-to-front draw order, which side faces
a view sees, whether a wall band is hidden by its neighbour (decision 5), and where a pin's name badge lands
(decision 7) - no Pillow above the line that splits this file. Below it, the drawing: the cutaway columns of a
floor, each floor's slab and wall layers drawn once per process, and the device, floor and house pictures composed
per request from them (decisions 2-9).
"""
from math import atan2, ceil, cos, degrees, floor, hypot, isfinite, pi, radians, sin

# ---------------------------------------------------------------------------------------------------------------
# Pure geometry (no Pillow above this part; the drawing follows it)
# ---------------------------------------------------------------------------------------------------------------


class View:
    """An orthographic axonometric camera at `azimuth_deg`/`elevation_deg` above the horizon, looking at the
    origin. `project` and `depth` share one right-handed basis (right, up, toward-camera) built from those two
    angles, so two parallel house edges always project to a parallel pair on screen.

    `explode` is the viewer's floor-gap multiplier (decision 3), carried on the view for the composing code to
    scale a floor's own z by (`floor.z * view.explode`, as `index.html`'s `base()` does) before calling
    `project`/`depth` - a single z value here can't tell which floor it belongs to, so `View` does not apply it.
    """

    def __init__(self, azimuth_deg, elevation_deg, explode=1.6):
        self.azimuth_deg, self.elevation_deg, self.explode = azimuth_deg, elevation_deg, explode
        az, el = radians(azimuth_deg), radians(elevation_deg)
        self._right = (-sin(az), cos(az), 0.0)
        self._up = (-cos(az) * sin(el), -sin(az) * sin(el), cos(el))
        self._toward = (cos(az) * cos(el), sin(az) * cos(el), sin(el))

    def project(self, x, y, z):
        """(u, v) screen metres: u right, v down - a higher z (nearer the ceiling) lands a smaller v."""
        rx, ry, rz = self._right
        ux, uy, uz = self._up
        return x * rx + y * ry + z * rz, -(x * ux + y * uy + z * uz)

    def depth(self, x, y, z):
        """Larger is nearer the camera: the point's distance along the camera's own viewing axis."""
        tx, ty, tz = self._toward
        return x * tx + y * ty + z * tz


# The viewer's default camera (index.html isoView(), k = 1): camera.position - houseCentre = (-11.5, +13, +13.1)
# in three.js scene axes (x east, scene y up, scene z = -y north) - 11.5 m west and 13.1 m south of the centre,
# 13 m up. VIEW_FLOOR shares that azimuth at a steeper elevation, about 55 degrees, so a single floor's rooms
# read from above (decision 3).
_WEST, _UP, _SOUTH = 11.5, 13.0, 13.1
_AZIMUTH_DEG = degrees(atan2(-_SOUTH, -_WEST))                     # bearing from the centre to the camera, x/y plane
_HOUSE_ELEVATION_DEG = degrees(atan2(_UP, hypot(_WEST, _SOUTH)))   # about 37 degrees up

VIEW_HOUSE = View(_AZIMUTH_DEG, _HOUSE_ELEVATION_DEG)
VIEW_FLOOR = View(_AZIMUTH_DEG, 55.0)


def draw_order(cells, W, view):
    """Raster keys (`r * W + c`, row 0 = north, as `house_api.house_payload` gives them) back to front for
    `view` (decision 5): a column's screen depth from its grid position alone, since every column rises from the
    same ground and a constant height offset would not change their relative order."""
    def screen_depth(key):
        r, c = divmod(key, W)
        return view.depth(c, -r, 0.0)
    return sorted(cells, key=screen_depth)


def visible_sides(view):
    """The two side faces `view`'s camera sees, of "n", "s", "e", "w" (decision 5): whichever of north/south and
    east/west sits nearer the camera - the side whose outward normal points back toward it."""
    az = radians(view.azimuth_deg)
    return ("s" if sin(az) < 0 else "n"), ("w" if cos(az) < 0 else "e")


def covered(band, neighbour_bands):
    """Whether a side face is hidden (decision 5): `band` and each of `neighbour_bands` are `(z0, z1, opaque)`;
    true when some opaque neighbour band spans `band`'s whole height."""
    z0, z1, _opaque = band
    return any(n_opaque and n0 <= z0 and n1 >= z1 for n0, n1, n_opaque in neighbour_bands)


def place_badges(anchors, sizes, bounds, taken=()):
    """Badge top-left per anchor (decision 7, greedy in anchor order): beside the anchor to the right, nudged
    down - then left, restarting from the top - until it overlaps no badge already placed, kept inside
    `bounds = (w, h)`. `taken`: boxes `(x, y, w, h)` already on the picture (a caption, the pins' heads) that no
    badge may cover either."""
    width, height = bounds
    gap, pad = 6.0, 2.0
    placed = [tuple(b) for b in taken]
    out = []
    for (ax, ay), (bw, bh) in zip(anchors, sizes):
        x0 = min(max(ax + gap, 0.0), max(0.0, width - bw))
        y0 = min(max(ay - bh / 2.0, 0.0), max(0.0, height - bh))
        step_x, step_y = bw + pad, bh + pad
        x, y, found = x0, y0, False
        while x >= -1e-9 and not found:
            y = y0
            while y <= height - bh + 1e-9:
                if not any(_boxes_overlap(x, y, bw, bh, *p) for p in placed):
                    found = True
                    break
                y += step_y
            if not found:
                x -= step_x
        if not found:
            x, y = 0.0, 0.0
        x = max(0.0, min(x, max(0.0, width - bw)))
        y = max(0.0, min(y, max(0.0, height - bh)))
        placed.append((x, y, bw, bh))
        out.append((x, y))
    return out


def _boxes_overlap(x1, y1, w1, h1, x2, y2, w2, h2):
    return x1 < x2 + w2 and x2 < x1 + w1 and y1 < y2 + h2 and y2 < y1 + h1


# ---------------------------------------------------------------------------------------------------------------
# Drawing (Pillow): each floor's base layers once per process, a picture composed per request (decisions 2-9)
# ---------------------------------------------------------------------------------------------------------------
import io                                       # noqa: E402
import threading                                # noqa: E402

import numpy as np                              # noqa: E402
from PIL import Image, ImageDraw, ImageFont     # noqa: E402

CUT = 1.2            # m: walls, windows, furniture and (on a floor's own picture) flights end here (decision 4)
WIDTH = 1024         # px, every picture; its height follows the content (decision 9)
PIN_H = 1.0          # m: a pin's head above its floor (decision 7)
SLAB = 0.15          # m: the slab's edge under each floor's rooms
RAMP_T = 0.25        # m: a flight's thickness, as the viewer's rampMesh
SS = 2               # base layers are drawn at twice the size and reduced: smooth edges, paid once per process
PAD, INSET = 22, 10  # px: the margin around a drawing; badges keep INSET inside the edges (corners transparent)
HEAD_R, DOT_R = 11, 4                                            # px: a pin's head and its dot on the slab
ROOM_PX, NAME_PX, CAPTION_PX, FLOOR_PX = 19, 22, 28, 24          # font sizes: legible on a card half as wide
PNG_LEVEL = 3        # zlib level: a third smaller than 1 for 2% more time (6: 7% smaller again, 17% slower)


def _rgb(c):
    return (c >> 16) & 255, (c >> 8) & 255, c & 255


def _mix(a, b, t):
    """Colour `a` at opacity `t` over colour `b`."""
    return tuple(round(x * t + y * (1 - t)) for x, y in zip(a, b))


def _shade(c, k, alpha=255):
    return tuple(min(255, round(x * k)) for x in c[:3]) + (alpha,)


# The viewer's palette (geometry.js VOX/FIX, geometry_logic.js PALETTE, index.html): a room is its floor's colour at
# the viewer's opacity over the viewer's dark scene background, made opaque, so light text on it reads on a light or
# a dark dashboard alike (decision 9).
BACKDROP = _rgb(0x0f141b)
PALETTE = [_rgb(c) for c in (0x3b82f6, 0x10b981, 0xf59e0b, 0xec4899, 0x8b5cf6, 0xef4444)]
WALLS = {"wall": _rgb(0x94a3b8), "exterior": _rgb(0x64748b)}
GLASS, GLASS_ALPHA = _rgb(0x7dd3fc), 115                        # the viewer's 45%
FIX = {k: _rgb(c) for k, c in dict(low=0xc8b89a, med=0xb08968, high=0x7a8fa6, water=0x38bdf8, wood=0xc8a97e,
                                   stone=0x9aa5b1, metal_top=0x5f6b7a, door=0xa0522d, wall=0x94a3b8,
                                   none=0x999999).items()}
THIN = ("wood", "stone", "metal_top")                            # the viewer draws tops 0.9, solids 0.45 opaque
PIN_COLOURS = {"phone": _rgb(0x22d3ee), "pet": _rgb(0xa3e635), "tag": _rgb(0xfacc15)}   # the viewer's DEV_COLORS
INK, TEXT, BADGE = (11, 15, 21), (226, 232, 240), (15, 23, 42, 235)
AWAY_GREY = (92, 92, 96)
FACES = (1.0, 0.8, 0.62)          # top, south, west: the viewer's sun is high in the south-east


def _faces(kind, _memo={}):
    """(top, south, west) RGBA of a band kind: walls slate, glass translucent blue, furniture muted, flights wood."""
    if kind not in _memo:
        alpha = GLASS_ALPHA if kind == "glass" else 255
        if kind == "glass":
            base = GLASS
        elif kind in WALLS:
            base = WALLS[kind]
        elif kind == "ramp":
            base = _mix(FIX["wood"], BACKDROP, 0.85)
        else:
            rf = kind[3:]
            base = _mix(FIX.get(rf, FIX["none"]), BACKDROP, 0.9 if rf in THIN else 0.7)
        faces = FACES if not kind.startswith("fx~") else (FACES[0],) + ((FACES[1] + FACES[2]) / 2,) * 2
        _memo[kind] = tuple(_shade(base, k, alpha) for k in faces)
    return _memo[kind]


def _slab_colour(fi, room, opacity=None):
    return _mix(PALETTE[fi % len(PALETTE)], BACKDROP, opacity or (0.08 if room["outdoor"] else 0.18))


# ---- the columns ----------------------------------------------------------------------------------------------
def columns(payload, fi, cut=CUT):
    """Per raster key of floor `fi` its bands `(z0, z1, kind)` above the slab, cut at `cut` (decisions 4, 5): walls
    ("wall" interior, "exterior"), an aperture's cell as wall below its sill, "glass" from a window's sill and
    nothing for a door or an opening; furniture footprints rasterised onto the same grid ("fx:<rf>", z_min to its
    top). A key with nothing below the cut is absent. Stair flights are not columns (`_ramp_strips`)."""
    f, res, vox = payload["floors"][fi], payload["res"], payload["voxels"][fi]
    out = {k: [(0.0, cut, "wall")] for k in vox.get("4", [])}
    out.update({k: [(0.0, cut, "exterior")] for k in vox.get("5", [])})
    bands = {b[0]: b for b in payload["bands"][fi]}
    outside =_exterior_apertures(bands, set(vox.get("5", [])), f["W"])
    for k, code, sill, head in bands.values():
        wall = "exterior" if k in outside else "wall"
        col = [(0.0, min(sill, cut), wall), (sill, min(head, cut), "glass" if code == 6 else None), (head, cut, wall)]
        col = [b for b in col if b[2] and b[1] > b[0]]
        if col:
            out[k] = col
        else:
            out.pop(k, None)
    walls = {k for code in ("4", "5", "6", "7", "8") for k in vox.get(code, [])}
    for fx in payload["fixtures"]:
        z0, z1 = max(0.0, fx["z_min"]), min(fx["height"], cut)
        if fx["floor"] != fi or fx["rf"] in (None, "none") or z1 <= z0:
            continue
        for k in _cells_in(fx["points"], f, res):
            if k not in walls:
                out.setdefault(k, []).append((z0, z1, "fx:" + fx["rf"]))
    for col in out.values():
        col.sort()
    return out


def _drawn_columns(payload, fi):
    """`columns` as the renderer draws them: furniture whose outline is not square to the grid (a round table, a
    recliner at 45 degrees) becomes "fx~<rf>", one shade on both sides, so its 5 cm steps do not read as stripes."""
    cols, f = columns(payload, fi), payload["floors"][fi]
    for fx in payload["fixtures"]:
        pts = fx["points"]
        if fx["floor"] != fi or all(abs(a[0] - b[0]) < 1e-6 or abs(a[1] - b[1]) < 1e-6
                                    for a, b in zip(pts, pts[1:] + pts[:1])):
            continue
        for k in _cells_in(pts, f, payload["res"]):
            if k in cols:
                cols[k] = [(z0, z1, "fx~" + kind[3:] if kind == "fx:" + str(fx["rf"]) else kind)
                           for z0, z1, kind in cols[k]]
    return cols


def _exterior_apertures(keys, exterior, W):
    """Aperture cells in an exterior wall (geometry_logic.js exteriorApertures): a connected aperture is exterior
    when any of its cells touches a plain exterior wall cell, so its sill is painted like that wall."""
    cells, seen, out = set(keys), set(), set()
    for start in cells:
        if start in seen:
            continue
        comp, stack, ext = [], [start], False
        seen.add(start)
        while stack:
            k = stack.pop()
            comp.append(k)
            for n in (k - 1, k + 1, k - W, k + W):
                ext = ext or n in exterior
                if n in cells and n not in seen:
                    seen.add(n)
                    stack.append(n)
        if ext:
            out.update(comp)
    return out


def _cells_in(points, f, res):
    """Raster keys whose cell centre lies inside the polygon (even-odd rule)."""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    c0, c1 = max(0, int((min(xs) - f["x0"]) / res)), min(f["W"] - 1, int((max(xs) - f["x0"]) / res))
    r0, r1 = max(0, int((f["ymax"] - max(ys)) / res)), min(f["H"] - 1, int((f["ymax"] - min(ys)) / res))
    if c1 < c0 or r1 < r0:
        return []
    cc, rr = np.meshgrid(np.arange(c0, c1 + 1), np.arange(r0, r1 + 1))
    px, py = f["x0"] + (cc + 0.5) * res, f["ymax"] - (rr + 0.5) * res
    inside = np.zeros(px.shape, bool)
    for (xi, yi), (xj, yj) in zip(points, points[-1:] + points[:-1]):
        if yi == yj:
            continue
        inside ^= ((yi > py) != (yj > py)) & (px < (xj - xi) * (py - yi) / (yj - yi) + xi)
    return (rr[inside] * f["W"] + cc[inside]).tolist()


def _runs(cols, W, H):
    """The columns as runs along each row (decision 5): cells side by side with one band list and the same south
    faces uncovered are one box per band. Drawn row by row from the north, east to west within a row, they stay in
    an exact painter's order - from the south-west a cell can only hide cells north and east of it. Returns
    (rows, sigs, tops): rows[r] = [(c0, c1, sig, south bits, west bits)] east first; sigs = the distinct band lists;
    tops[sig] = bits of its bands whose top is not under another band."""
    sigs, index = [()], {}
    grid = np.zeros(W * H, np.int64)
    for k, bands in cols.items():
        t = tuple(bands)
        if t not in index:
            index[t] = len(sigs)
            sigs.append(t)
        grid[k] = index[t]
    grid = grid.reshape(H, W)
    south = np.zeros_like(grid)
    south[:-1] = grid[1:]
    pairs, inverse = np.unique(grid * len(sigs) + south, return_inverse=True)
    bits = np.array([_uncovered(sigs[p // len(sigs)], sigs[p % len(sigs)]) for p in pairs.tolist()])
    sbits = bits[inverse.reshape(-1)].reshape(H, W)
    key = grid * 1024 + sbits
    west, rows = {}, []
    for r in range(H):
        kr = key[r]
        starts = np.flatnonzero(np.r_[True, kr[1:] != kr[:-1]])
        ends = np.r_[starts[1:], W] - 1
        row = []
        for c0, c1 in zip(starts.tolist(), ends.tolist()):
            sig = int(grid[r, c0])
            if sig:
                w = (sig, int(grid[r, c0 - 1]) if c0 else 0)
                if w not in west:
                    west[w] = _uncovered(sigs[w[0]], sigs[w[1]])
                row.append((c0, c1, sig, int(sbits[r, c0]), west[w]))
        rows.append(row[::-1])
    tops = [sum(1 << i for i, (_, z1, _) in enumerate(s) if not any(abs(o - z1) < 1e-6 for o, _, _ in s))
            for s in sigs]
    return rows, sigs, tops


def _uncovered(bands, neighbour):
    """Bits of `bands` whose side face the neighbour's bands do not hide (`covered`; glass hides nothing)."""
    nb = [(z0, z1, kind != "glass") for z0, z1, kind in neighbour]
    return sum(1 << i for i, (z0, z1, _) in enumerate(bands) if not covered((z0, z1, True), nb))


# ---- stair flights --------------------------------------------------------------------------------------------
def _ramp_profile(rp, cut):
    """A flight's side profile [(t, top, bottom)] along its run axis, ascending t: a slab RAMP_T thick from the
    foot up to the landing's end (the viewer's rampMesh), never below its floor, cut at `cut` (None: whole)."""
    (c0, c1), z0, z1 = rp["run"], rp["z0"], rp["z1"]
    lo, hi = (rp["x0"], rp["x1"]) if rp["axis"] == "x" else (rp["y0"], rp["y1"])
    up = 1.0 if c1 >= c0 else -1.0
    end = hi if up > 0 else lo
    ts = {c0, c1, end}
    for z in (RAMP_T,) + (() if cut is None else (cut, cut + RAMP_T)):
        if min(z0, z1) < z < max(z0, z1):
            ts.add(c0 + (c1 - c0) * (z - z0) / (z1 - z0))
    out = []
    for t in sorted(ts, key=lambda t: (t - c0) * up):
        if (t - c0) * up < 0 or (t - end) * up > 1e-9:
            continue
        top = z1 if (t - c1) * up >= 0 else z0 + (z1 - z0) * (t - c0) / (c1 - c0)
        roof, bottom = top if cut is None else min(top, cut), max(0.0, top - RAMP_T)
        if roof < bottom - 1e-9:
            break
        out.append((t, roof, bottom))
    return sorted(out)


def _at(profile, t):
    """(top, bottom) of a profile at t, or None outside it."""
    for (ta, ha, la), (tb, hb, lb) in zip(profile, profile[1:]):
        if ta - 1e-9 <= t <= tb + 1e-9:
            k = 0.0 if tb == ta else (t - ta) / (tb - ta)
            return ha + (hb - ha) * k, la + (lb - la) * k
    return None


def _ramp_strips(rp, f, res, cut, walls):
    """A flight as one strip per raster row it crosses, each a few 3-D polygons (west end, sloped top, treads, and
    on the southernmost strip its side), so it takes its place in the rows' painter's order like a wall run: walls
    in front of it cover it, walls behind it do not (decision 3: stair flights as wooden ramps). A strip is trimmed
    to its row's cells clear of `walls` (keys): the well's outline runs through the walls around it.
    Returns {row: [(west column, [(points, rgba)])]}."""
    prof = _ramp_profile(rp, cut)
    if len(prof) < 2:
        return {}
    top, south, west = _faces("ramp")
    tread = _shade(top, 0.62)
    (c0, c1), n = rp["run"], max(1, rp["treads"])
    treads = [c0 + (c1 - c0) * i / n for i in range(1, n + 1)]
    along_x = rp["axis"] == "x"
    ya, yb = (rp["y0"], rp["y1"]) if along_x else (prof[0][0], prof[-1][0])
    xa, xb = (prof[0][0], prof[-1][0]) if along_x else (rp["x0"], rp["x1"])
    strips = []
    for r in range(max(0, floor((f["ymax"] - yb) / res)), min(f["H"], ceil((f["ymax"] - ya) / res))):
        yn, ys = min(yb, f["ymax"] - r * res), max(ya, f["ymax"] - (r + 1) * res)
        ca, cb = max(0, floor((xa - f["x0"]) / res)), min(f["W"] - 1, ceil((xb - f["x0"]) / res) - 1)
        while ca <= cb and r * f["W"] + ca in walls:
            ca += 1
        while cb >= ca and r * f["W"] + cb in walls:
            cb -= 1
        xw, xe = max(xa, f["x0"] + ca * res), min(xb, f["x0"] + (cb + 1) * res)
        if yn <= ys or xe <= xw:
            continue
        if along_x:
            p = [(xw,) + _at(prof, xw)] + [q for q in prof if xw < q[0] < xe] + [(xe,) + _at(prof, xe)]
            polys = [([(xw, ys, p[0][2]), (xw, yn, p[0][2]), (xw, yn, p[0][1]), (xw, ys, p[0][1])], west)]
            polys += [([(ta, ys, ha), (tb, ys, hb), (tb, yn, hb), (ta, yn, ha)], top)
                      for (ta, ha, _), (tb, hb, _) in zip(p, p[1:])]
            polys += [([(t, ys, z[0]), (t, yn, z[0])], tread) for t in treads if xw < t < xe and (z := _at(prof, t))]
            side = [(t, ys, h) for t, h, _ in p] + [(t, ys, lo) for t, _, lo in p[::-1]]
        else:
            a, b = _at(prof, ys), _at(prof, yn)
            if not (a and b):
                continue
            polys = [([(xw, ys, a[1]), (xw, yn, b[1]), (xw, yn, b[0]), (xw, ys, a[0])], west),
                     ([(xw, ys, a[0]), (xe, ys, a[0]), (xe, yn, b[0]), (xw, yn, b[0])], top)]
            polys += [([(xw, t, z[0]), (xe, t, z[0])], tread) for t in treads if ys <= t < yn and (z := _at(prof, t))]
            side = [(xw, ys, a[1]), (xe, ys, a[1]), (xe, ys, a[0]), (xw, ys, a[0])]
        strips.append((r, ca, polys, side))
    out = {}
    for i, (r, ca, polys, side) in enumerate(strips):
        out.setdefault(r, []).append((ca, polys + ([(side, south)] if i == len(strips) - 1 else [])))
    return out


# ---- frames, sprites, badges -----------------------------------------------------------------------------------
def _px(v):
    return int(floor(v + 0.5))


class _Frame:
    """A picture's geometry: its view, each drawn floor's base height (exploded in the house picture), the scale that
    fits `points` [(fi, x, y, h)] into WIDTH between the margins `pad` (left, top, right, bottom), and its height."""

    def __init__(self, view, zbase, points, pad=(PAD, PAD, PAD, PAD)):
        self.view, self.zbase = view, zbase
        uv = [view.project(x, y, zbase[fi] + h) for fi, x, y, h in points]
        u0, u1 = min(u for u, _ in uv), max(u for u, _ in uv)
        v0, v1 = min(v for _, v in uv), max(v for _, v in uv)
        left, top, right, bottom = pad
        self.s = (WIDTH - left - right) / max(u1 - u0, 1e-6)
        self.ox, self.oy = left - self.s * u0, top - self.s * v0
        self.w, self.h = WIDTH, int(ceil(self.s * (v1 - v0) + top + bottom))

    def xy(self, fi, x, y, h=0.0, k=1):
        u, v = self.view.project(x, y, self.zbase[fi] + h)
        return (self.ox + self.s * u) * k, (self.oy + self.s * v) * k

    def affine(self, fi, k):
        """(ax, ay, a0, bx, by, bz, b0): px = ax x + ay y + a0, py = bx x + by y + bz h + b0 on floor fi."""
        (ox, oy), (ex, ey), (nx, ny), (_, zy) = (self.xy(fi, *p, k=k) for p in ((0, 0), (1, 0), (0, 1), (0, 0, 1)))
        return ex - ox, nx - ox, ox, ey - oy, ny - oy, zy - oy, oy


def _extent(payload, fi):
    """Floor fi's drawn area (x0, y0, x1, y1): its rooms and its wall cells."""
    f, res = payload["floors"][fi], payload["res"]
    xs = [p[0] for r in payload["rooms"] if r["floor"] == fi for p in r["points"]]
    ys = [p[1] for r in payload["rooms"] if r["floor"] == fi for p in r["points"]]
    keys = [k for code in ("4", "5", "6", "7", "8") for k in payload["voxels"][fi].get(code, [])]
    if keys or not xs:
        rr, cc = divmod(np.array(keys or [0, f["W"] * f["H"] - 1]), f["W"])
        xs += [f["x0"] + cc.min() * res, f["x0"] + (cc.max() + 1) * res]
        ys += [f["ymax"] - (rr.max() + 1) * res, f["ymax"] - rr.min() * res]
    return min(xs), min(ys), max(xs), max(ys)


def _font(size, _memo={}):
    if size not in _memo:
        _memo[size] = ImageFont.load_default(size=size)       # Pillow's own scalable font: no font files to ship
    return _memo[size]


def _supersampled(w, h, paint, k=4):
    """An RGBA image w x h painted at k times the size and reduced: smooth edges for the small shapes."""
    big = Image.new("RGBA", (w * k, h * k), (0, 0, 0, 0))
    paint(ImageDraw.Draw(big), k)
    return big.resize((w, h), Image.Resampling.BOX)


def _disc(colour, r, ring, _memo={}):
    """A round head or dot, 2r + 1 px across with a dark ring; its centre pixel is exactly `colour`."""
    if (colour, r, ring) not in _memo:
        n = 2 * r + 1
        def paint(d, k):
            d.ellipse([0, 0, n * k - 1, n * k - 1], fill=INK + (255,))
            d.ellipse([ring * k, ring * k, (n - ring) * k - 1, (n - ring) * k - 1], fill=tuple(colour) + (255,))
        _memo[(colour, r, ring)] = _supersampled(n, n, paint)
    return _memo[(colour, r, ring)]


def _badge(text, size, dot=None):
    """Light text on a dark rounded badge (decision 9), with a dot of the pin's colour when it names a pin."""
    font = _font(size)
    asc, desc = font.getmetrics()
    tw = font.getbbox(text)[2]
    pad, dr = round(size * 0.45), (round(size * 0.27) if dot else 0)
    w, h = int(pad + (2 * dr + pad * 0.6 if dot else 0) + tw + pad), int(asc + desc + 2 * round(size * 0.22))

    def paint(d, k):
        d.rounded_rectangle([0, 0, w * k - 1, h * k - 1], radius=h * k * 0.32, fill=BADGE)
        if dot:
            cx, cy = (pad + dr) * k, h * k / 2
            d.ellipse([cx - dr * k, cy - dr * k, cx + dr * k, cy + dr * k], fill=tuple(dot) + (255,))
    im = _supersampled(w, h, paint)
    ImageDraw.Draw(im).text((w - pad - tw, h / 2), text, font=font, fill=TEXT + (255,), anchor="lm")
    return im


def _paste(im, sprite, x, y):
    """alpha_composite `sprite` with its top-left at (x, y), cropped to the picture."""
    x, y = _px(x), _px(y)
    left, top = max(0, -x), max(0, -y)
    right, bottom = min(sprite.width, im.width - x), min(sprite.height, im.height - y)
    if right > left and bottom > top:
        im.alpha_composite(sprite, (x + left, y + top), (left, top, right, bottom))


def _png(im):
    buf = io.BytesIO()
    im.save(buf, "PNG", compress_level=PNG_LEVEL)
    return buf.getvalue()


def _area(points):
    """Signed: positive when the outline runs anticlockwise."""
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])) / 2


def _number(v):
    return isinstance(v, (int, float)) and isfinite(v)


def _placed(m):
    """A mark with a pin: present, on a floor, at a position."""
    return m.get("status") != "away" and m.get("floor_id") is not None and _number(m.get("x")) and _number(m.get("y"))


def _house_explode(payload, cover=0.15, grid=0.1):
    """The house picture's floor gap (decision 3, review fix): the viewer's explode at least, raised in steps of 0.05
    until the floors above hide at most `cover` of any floor's slab - its far, north part - so every floor's rooms
    and pins stand on slab the picture shows. Measured on the room outlines projected onto a `grid` m raster.
    0.15, not a quarter: on Nick's house a quarter (explode 1.85) still hid the Garage's and the Kitchen's middles,
    a far quarter by area reaching deep where a floor's diamond narrows; 0.15 (explode 2.2) hides neither."""
    view, floors = VIEW_HOUSE, payload["floors"]
    outlines = [[[view.project(x, y, 0.0) for x, y in r["points"]] for r in payload["rooms"] if r["floor"] == fi]
                for fi in range(len(floors))]
    pts = [p for rooms in outlines for poly in rooms for p in poly]
    if len(floors) < 2 or not pts:
        return view.explode
    u0, v0 = min(u for u, _ in pts), min(v for _, v in pts)
    size = (int((max(u for u, _ in pts) - u0) / grid) + 2, int((max(v for _, v in pts) - v0) / grid) + 2)
    slabs = []
    for rooms in outlines:
        m = Image.new("1", size)
        for poly in rooms:
            ImageDraw.Draw(m).polygon([((u - u0) / grid, (v - v0) / grid) for u, v in poly], fill=1)
        slabs.append(np.asarray(m))
    rise, rows_of = cos(radians(view.elevation_deg)) / grid, size[1]      # raster rows per metre of height

    def raised(m, n):                                        # a slab n rows up the picture
        out = np.zeros_like(m)
        if n < rows_of:
            out[:rows_of - n] = m[n:]
        return out
    edge, explode = max(1, round(SLAB * rise)), view.explode
    while explode < 8.0:
        hidden = []
        for i, lower in enumerate(slabs):
            over = np.zeros_like(lower)
            for j in range(i + 1, len(slabs)):
                n = round((floors[j]["z"] - floors[i]["z"]) * explode * rise)
                for t in range(max(0, n - edge), n + 1):     # the slab above and its edge
                    over |= raised(slabs[j], t)
            hidden.append((lower & over).sum() / max(1, lower.sum()))
        if max(hidden) <= cover:
            break
        explode = round(explode + 0.05, 2)
    return explode


# ---- the renderer ---------------------------------------------------------------------------------------------
class Renderer:
    """The house's pictures (decisions 2-6): `warm()` draws each floor's slab and wall layers for the floor and
    house views once (a house is fixed for the life of an engine process); `device`, `floor` and `house` compose a
    PNG from them per request - tint, radius disc, walls, labels, pins and badges. Thread-safe: one lock covers the
    warm-up and each composition."""

    def __init__(self, payload):
        self.payload = payload
        self._ix = {f["id"]: fi for fi, f in enumerate(payload["floors"])}
        self._lock = threading.RLock()
        self._ready = False
        self._floors, self._house = {}, None

    @property
    def ready(self):
        return self._ready

    @property
    def floor_ids(self):
        return [f["id"] for f in self.payload["floors"]]

    def pixel(self, floor_id, x, y, h=0.0, house=False):
        """Where a point h m above floor `floor_id` lands in that floor's (or the house's) picture, in pixels."""
        self.warm()
        fi = self._index(floor_id)
        fr = self._house[0] if house else self._floors[fi][0]
        return tuple(_px(v) for v in fr.xy(fi, x, y, h))

    def warm(self):
        with self._lock:
            if self._ready:
                return
            p = self.payload
            geo = [_runs(_drawn_columns(p, fi), f["W"], f["H"]) for fi, f in enumerate(p["floors"])]
            box = self._extents = [_extent(p, fi) for fi in range(len(p["floors"]))]
            corners = lambda b: ((b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3]))  # noqa: E731
            # a floor's own pictures keep a band above the house for the device map's caption: nothing drawn,
            # not even a pin's head at the far corner, can sit under it
            band = INSET + _badge("Ag", CAPTION_PX).height + HEAD_R + 4
            for fi in range(len(p["floors"])):
                fr = _Frame(VIEW_FLOOR, {fi: 0.0}, [(fi, x, y, h) for x, y in corners(box[fi])
                                                    for h in (-SLAB, max(CUT, PIN_H))], (PAD, band, PAD, PAD))
                walls = self._walls(fr, fi, geo[fi], CUT)
                self._room_names(walls, fr, fi)
                self._floors[fi] = (fr, self._slab(fr, fi), walls)
            # the house, exploded at least as in the viewer and enough to keep each floor's slab mostly in view;
            # flights whole, rising into the gap toward their well
            explode = _house_explode(p)
            zbase = {fi: f["z"] * explode for fi, f in enumerate(p["floors"])}
            labels = [_badge(f["name"], FLOOR_PX) for f in p["floors"]]
            tall = {fi: max([CUT, PIN_H] + [rp["z1"] for rp in p.get("ramps", []) if rp["lower"] == fi])
                    for fi in zbase}
            pad = (INSET + max(b.width for b in labels) + 12, PAD, PAD, PAD)
            fr = _Frame(VIEW_HOUSE, zbase, [(fi, x, y, h) for fi in zbase for x, y in corners(box[fi])
                                            for h in (-SLAB, tall[fi])], pad)
            base = Image.new("RGBA", (fr.w, fr.h), (0, 0, 0, 0))
            spots = []
            for fi in zbase:
                base.alpha_composite(self._slab(fr, fi))
                base.alpha_composite(self._walls(fr, fi, geo[fi], None))
                lx, ly = min((fr.xy(fi, x, y) for x, y in corners(box[fi])), key=lambda q: q[0])
                spots.append((max(INSET, lx - 12 - labels[fi].width), ly - labels[fi].height / 2))
            self._house = (fr, base, list(zip(labels, spots)))
            self._ready = True

    # ---- base layers (once) ----
    def _slab(self, fr, fi):
        """The floor's rooms at z = 0 in its tint, over their edges SLAB deep where they face the camera, and the
        well of a flight from below outlined."""
        k, rooms = SS, [r for r in self.payload["rooms"] if r["floor"] == fi]
        im = Image.new("RGBA", (fr.w * k, fr.h * k), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        az = radians(fr.view.azimuth_deg)
        for r in rooms:
            pts, edge = r["points"], _shade(_slab_colour(fi, r), 0.55)
            for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
                nx, ny = (y2 - y1, x1 - x2) if _area(pts) > 0 else (y1 - y2, x2 - x1)     # outward
                if nx * cos(az) + ny * sin(az) > 1e-9:
                    d.polygon([fr.xy(fi, x1, y1, 0, k), fr.xy(fi, x2, y2, 0, k), fr.xy(fi, x2, y2, -SLAB, k),
                               fr.xy(fi, x1, y1, -SLAB, k)], fill=edge)
        for r in rooms:
            d.polygon([fr.xy(fi, x, y, 0, k) for x, y in r["points"]], fill=_slab_colour(fi, r) + (255,))
        for rp in self.payload.get("ramps", []):
            if rp["lower"] + 1 == fi:
                q = [(rp["x0"], rp["y0"]), (rp["x1"], rp["y0"]), (rp["x1"], rp["y1"]), (rp["x0"], rp["y1"])]
                d.line([fr.xy(fi, x, y, 0, k) for x, y in q + q[:1]], fill=_faces("ramp")[0], width=2 * k)
        return im.resize((fr.w, fr.h), Image.Resampling.BOX)

    def _walls(self, fr, fi, geo, ramp_cut):
        """The floor's columns and flights in painter's order (decision 5): rows from the north, east to west in a
        row; per box its uncovered south and west faces and its top, band by band from the floor up."""
        rows, sigs, tops = geo
        f, res, k = self.payload["floors"][fi], self.payload["res"], SS
        strips, vox = {}, self.payload["voxels"][fi]
        walls = {key for code in ("4", "5", "6") for key in vox.get(code, [])}
        for rp in self.payload.get("ramps", []):
            if rp["lower"] == fi:
                for r, items in _ramp_strips(rp, f, res, ramp_cut, walls).items():
                    strips.setdefault(r, []).extend(items)
        im = Image.new("RGBA", (fr.w * k, fr.h * k), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        poly, line = d.polygon, d.line
        ax, ay, a0, bx, by, bz, b0 = fr.affine(fi, k)
        at = lambda x, y: (ax * x + ay * y + a0, bx * x + by * y + b0)     # noqa: E731 - (x, y, 0) in pixels
        hair = 1.0 / (fr.s * k)            # m, about a pixel: see below
        faces = [[_faces(kind) for _, _, kind in s] for s in sigs]
        for r, row in enumerate(rows):
            yn = f["ymax"] - r * res
            ys = yn - res
            if r in strips:
                row = sorted(row + strips[r], key=lambda it: -it[0])
            for item in row:
                if len(item) == 2:                                   # a flight's strip
                    for pts, rgba in item[1]:
                        xy = [(ax * x + ay * y + a0, bx * x + by * y + bz * z + b0) for x, y, z in pts]
                        if len(xy) == 2:
                            line(xy, fill=rgba, width=k)
                        else:
                            poly(xy, fill=rgba)
                    continue
                c0, c1, sig, sb, wb = item
                xa, xb = f["x0"] + c0 * res, f["x0"] + (c1 + 1) * res
                swx, swy = at(xa, ys)
                # Pillow's fill can leave a hairline on an edge two polygons share, so each face reaches a hair
                # past its east and north edges (into faces drawn before it) and its top edge (under the face
                # drawn next): the hair is painted over, never left as a gap
                (sex, sey), (nwx, nwy), (nex, ney) = at(xb + hair, ys), at(xa, yn + hair), at(xb + hair, yn + hair)
                for i, (z0, z1, _) in enumerate(sigs[sig]):
                    top, south, west = faces[sig][i]
                    h0, h1, up = bz * z0, bz * z1, bz * z1 - 1.0
                    if sb >> i & 1:
                        poly([(swx, swy + h0), (sex, sey + h0), (sex, sey + up), (swx, swy + up)], fill=south)
                    if wb >> i & 1:
                        poly([(swx, swy + h0), (nwx, nwy + h0), (nwx, nwy + up), (swx, swy + up)], fill=west)
                    if tops[sig] >> i & 1:
                        poly([(swx, swy + h1), (sex, sey + h1), (nex, ney + h1), (nwx, nwy + h1)], fill=top)
        return im.resize((fr.w, fr.h), Image.Resampling.BOX)

    def _room_names(self, im, fr, fi):
        """Room names on a floor's own pictures (decision 2): light text with a dark edge on the slab, largest
        rooms first; a name that would run into one already written is left out (a closet's, usually)."""
        d, font, written = ImageDraw.Draw(im), _font(ROOM_PX), []
        rooms = sorted((r for r in self.payload["rooms"] if r["floor"] == fi), key=lambda r: -abs(_area(r["points"])))
        for r in rooms:
            at = fr.xy(fi, *r["centroid"])
            box = d.textbbox(at, r["name"], font=font, anchor="mm", stroke_width=3)
            if not any(_boxes_overlap(box[0], box[1], box[2] - box[0], box[3] - box[1], *b) for b in written):
                written.append((box[0] - 4, box[1] - 2, box[2] - box[0] + 8, box[3] - box[1] + 4))
                d.text(at, r["name"], font=font, fill=TEXT + (255,), anchor="mm", stroke_width=3,
                       stroke_fill=INK + (255,))

    # ---- pictures (per request) ----
    def floor(self, floor_id, marks):
        """Floor `floor_id` with room names and a pin and name for every mark placed on it (decision 2)."""
        fi = self._index(floor_id)
        with self._lock:
            self.warm()
            fr, slab, walls = self._floors[fi]
            im = slab.copy()
            im.alpha_composite(walls)
            self._pins(im, fr, [(fi, m) for m in marks if _placed(m) and m["floor_id"] == floor_id], [])
            return _png(im)

    def house(self, marks):
        """Every floor exploded, every placed mark's pin, the floors' names beside them (decision 2)."""
        with self._lock:
            self.warm()
            fr, base, labels = self._house
            im = base.copy()
            taken = [(x, y, b.width, b.height) for b, (x, y) in labels]
            self._pins(im, fr, [(self._ix[m["floor_id"]], m) for m in marks
                                if _placed(m) and m["floor_id"] in self._ix], taken)
            for b, (x, y) in labels:
                _paste(im, b, x, y)
            return _png(im)

    def device(self, mark):
        """A device's map (decision 2): its floor, its room tinted, the r68 disc and its pin, a caption badge. Away:
        the last floor with the last room grey and no pin; no estimate yet: a small picture with the badge only."""
        name = str(mark.get("name") or mark.get("key") or "")
        with self._lock:
            self.warm()
            if mark.get("status") == "away":
                since = mark.get("since")
                caption = f"{name} · Not detected" + (f" since {since}" if since else "")
                fid = mark.get("last_floor_id")
                if fid not in self._ix:
                    return self._caption_only(caption)
                fi = self._ix[fid]
                return self._device_map(fi, None, caption, mark.get("last_room"), AWAY_GREY)
            if not _placed(mark):
                return self._caption_only(f"{name} · Locating...")
            fi = self._index(mark["floor_id"])
            room = mark.get("room")
            caption = f"{name} · {room or self.payload['floors'][fi]['name']}"
            return self._device_map(fi, mark, caption, room, _mix(PALETTE[fi % len(PALETTE)], BACKDROP, 0.5))

    def _device_map(self, fi, mark, caption, room, tint):
        fr, slab, walls = self._floors[fi]
        im = slab.copy()
        d = ImageDraw.Draw(im)
        for r in self.payload["rooms"]:
            if r["floor"] == fi and r["name"] == room:
                d.polygon([fr.xy(fi, x, y) for x, y in r["points"]], fill=tuple(tint) + (255,))
        if mark:
            self._radius(im, fr, fi, mark)
        im.alpha_composite(walls)
        self._pins(im, fr, [(fi, mark)] if mark else [], [], names=False)      # the caption names it
        _paste(im, _badge(caption, CAPTION_PX), INSET, INSET)
        return _png(im)

    def _radius(self, im, fr, fi, mark):
        """The estimate's 68% disc on the floor, translucent in the pin's colour (decision 2). A radius that is not a
        finite positive number is left out and one wider than the floor drawn as wide as the floor; only the part on
        the picture is painted, more coarsely when it is large: a request's cost stays bounded (review fix)."""
        r68 = mark.get("r68")
        if not _number(r68) or r68 <= 0:
            return
        x0, y0, x1, y1 = self._extents[fi]
        r68 = min(r68, hypot(x1 - x0, y1 - y0))
        colour = PIN_COLOURS.get(mark.get("kind"), PIN_COLOURS["tag"])
        ring = [fr.xy(fi, mark["x"] + r68 * cos(a), mark["y"] + r68 * sin(a))
                for a in (2 * pi * i / 64 for i in range(64))]
        xs, ys = [x for x, _ in ring], [y for _, y in ring]
        left, top = max(0, floor(min(xs)) - 2), max(0, floor(min(ys)) - 2)
        right, bottom = min(im.width, ceil(max(xs)) + 3), min(im.height, ceil(max(ys)) + 3)
        if right <= left or bottom <= top:
            return
        w, h = right - left, bottom - top

        def paint(d, k):
            d.polygon([((x - left) * k, (y - top) * k) for x, y in ring], fill=tuple(colour) + (85,),
                      outline=tuple(colour) + (210,), width=2 * k)
        _paste(im, _supersampled(w, h, paint, 4 if w * h <= 300 ** 2 else 2 if w * h <= 700 ** 2 else 1), left, top)

    def _pins(self, im, fr, pins, taken, names=True):
        """Stems and dots, then heads, then name badges, over every floor (decision 7); a badge never covers a
        head or a floor name, and keeps INSET inside the picture."""
        d = ImageDraw.Draw(im)
        heads = []
        for fi, m in pins:
            colour = PIN_COLOURS.get(m.get("kind"), PIN_COLOURS["tag"])
            (gx, gy), (hx, hy) = fr.xy(fi, m["x"], m["y"]), fr.xy(fi, m["x"], m["y"], PIN_H)
            d.line([(gx, gy), (hx, hy)], fill=INK + (255,), width=5)
            d.line([(gx, gy), (hx, hy)], fill=tuple(colour) + (255,), width=3)
            _paste(im, _disc(colour, DOT_R, 1.5), _px(gx) - DOT_R, _px(gy) - DOT_R)
            heads.append((hx, hy, colour, m))
        for hx, hy, colour, _ in heads:
            _paste(im, _disc(colour, HEAD_R, 2.5), _px(hx) - HEAD_R, _px(hy) - HEAD_R)
        if not names:
            return
        badges = [_badge(str(m.get("name") or m.get("key") or ""), NAME_PX, dot=colour) for _, _, colour, m in heads]
        taken = list(taken) + [(hx - HEAD_R, hy - HEAD_R, 2 * HEAD_R + 1, 2 * HEAD_R + 1) for hx, hy, _, _ in heads]
        spots = place_badges([(hx + HEAD_R + 2 - INSET, hy - INSET) for hx, hy, _, _ in heads],
                             [(b.width, b.height) for b in badges], (im.width - 2 * INSET, im.height - 2 * INSET),
                             taken=[(x - INSET, y - INSET, w, h) for x, y, w, h in taken])
        for b, (x, y) in zip(badges, spots):
            _paste(im, b, x + INSET, y + INSET)

    def _caption_only(self, caption):
        """No floor to show (no estimate yet, or away with no last floor): the caption alone, a small picture."""
        badge = _badge(caption, CAPTION_PX)
        im = Image.new("RGBA", (WIDTH, badge.height + 2 * PAD), (0, 0, 0, 0))
        _paste(im, badge, max(INSET, (WIDTH - badge.width) // 2), PAD)
        return _png(im)

    def _index(self, floor_id):
        if floor_id not in self._ix:
            raise KeyError(floor_id)
        return self._ix[floor_id]
