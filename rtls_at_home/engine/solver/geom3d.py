"""Vectorised 3-D geometry features for BLE path-loss models.

For every (scanner, target point) pair this computes the things a propagation model
needs, once, so that fitting and localisation reduce to cheap array arithmetic:

  d3, dxy, dz          straight-line distances
  k4 k5 k6 k7 k8       crossings of interior wall / exterior wall / glass / door / metal door
  kh                   entries into a high-attenuation fixture (metal, water) above the ray
  kslab                floor/ceiling assemblies crossed
  G                    door-routed geodesic distance on the target's floor

The ray-march is genuinely 3-D: at every sample it decides which floor the ray is in
from its height, and reads *that* floor's raster. The previous model never wall-marched
cross-floor paths at all, and reported walls=0 for them - which meant "not checked".
"""
import json, math, os, hashlib, importlib.util
from dataclasses import dataclass
from datetime import datetime
import numpy as np
import cv2
from PIL import Image
from scipy import ndimage
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra

import house_doc as HD
import house_build as HB

HERE = os.path.dirname(os.path.abspath(__file__))
RES = 0.05
def cache_dir():
    """Where feature-bank caches go: RP_CACHE_DIR, else <RTLS_SESSIONS>/cache (the data folder, so an applied house's
    geometry survives the container being replaced), else solver/ (where the image bakes them at deploy)."""
    if os.environ.get("RP_CACHE_DIR"):
        return os.environ["RP_CACHE_DIR"]
    s = os.environ.get("RTLS_SESSIONS")
    return os.path.join(s, "cache") if s else HERE
RF_CODE = {"none": 0, "low": 1, "med": 2, "high": 3, "wall": 4, "water": 9,   # water: reef tank, ATO (2026-09-25)
           "wood": 10, "stone": 11, "metal_top": 12}                              # thin tops / sheets (layers, 2026-09-26)
PASSABLE = (0, 1, 2, 3, 7)          # free, fixtures, interior doors: routable
SAMPLES = 400                        # <= 4 cm steps on the longest in-house ray
GUARD_SRC = GUARD_DST = float(os.environ.get("RP_GUARD", "0.10"))   # see patch_three: 0.10 exempts a mounting wall


@dataclass
class Ramp:
    """A flight of stairs as a sloped slab (Nick 2026-09-26): inside the well opening (x0..x1, y0..y1 in the
    UPPER floor's slab) the assembly between floors `lower` and `lower + 1` is the flight itself, rising from
    z_bottom at c_bottom to z_top at c_top along run_axis, flat beyond both ends (the foot, the landing).
    Outside the opening the flat slab at z_top stands. Rays cross the flight where they cross that surface;
    the open air above it belongs to whichever floor the height says, so a ray up the well crosses nothing."""
    lower: int
    x0: float; y0: float; x1: float; y1: float
    run_axis: str
    z_bottom: float; z_top: float
    c_bottom: float = None; c_top: float = None

    def __post_init__(self):
        lo, hi = (self.x0, self.x1) if self.run_axis == "x" else (self.y0, self.y1)
        if self.c_bottom is None: self.c_bottom = lo
        if self.c_top is None: self.c_top = hi

    @property
    def angle_deg(self):
        return float(np.degrees(np.arctan2(self.z_top - self.z_bottom, abs(self.c_top - self.c_bottom))))

    def inside(self, X, Y):
        return (X >= self.x0) & (X <= self.x1) & (Y >= self.y0) & (Y <= self.y1)

    def surface(self, X, Y):
        """Height of the flight under (X, Y), extended flat beyond its ends: continuous, so a ray entering the
        well at its foot or side is not charged the well's edge."""
        c = X if self.run_axis == "x" else Y
        t = np.clip((c - self.c_bottom) / (self.c_top - self.c_bottom), 0.0, 1.0)
        return self.z_bottom + (self.z_top - self.z_bottom) * t


RAMP_EPS = 0.03                     # m under the ramp surface that still counts as "on the tread"
USE_RAMPS = os.environ.get("RP_RAMPS", "1") == "1"   # experiment switch (flags.py): 0 = flat slabs over the wells
ASSEMBLY = 0.33                     # floor assembly depth, m
# RP_RAYS=exact: wall/fixture features from exact per-cell traversal instead of SAMPLES points per ray
EXACT = os.environ.get("RP_RAYS", "sample") == "exact"
# Horizontal plates (RP_PLATES=1 in models.py; features need RP_RAYS=exact): furniture TOPS as thin slabs
# of a known material over the fixture footprint - a path under or over the table crosses nothing, a path
# through the top crosses thickness / sin(slope). Legs, frames and chair bodies are deliberately ignored.
# Materials and thicknesses are GUESSES (2026-09-24) until Nick measures them.
PLATE_MATERIALS = ("wood", "stone", "metal")          # raster codes 1, 2, 3; plates are objects' `plate` (house.json)
# Grazing cap, as for the floor assembly (mslab): a path is charged at most PLATE_CAP x thickness per
# plate crossing. A shallow path from a phone lying ON a table to a distant low scanner really does run
# ~0.7 m inside a 4 cm top, but real signal goes over the edge. 0 disables the cap.
PLATE_CAP = float(os.environ.get("RP_PLATE_CAP", "3.0"))


class House:
    def __init__(self, doc=None, build_dir=None):
        """The house from its document (house_doc.load() by default) and the maps built from it (house_build)."""
        self.doc = doc if doc is not None else HD.load()
        self.build_dir = build_dir or HB.ensure_build(self.doc)
        fr = self.doc["frame"]
        self.X0, self.Y0 = float(fr.get("x0", 0.0)), float(fr.get("y0", 0.0))
        self.ids = [F["id"] for F in self.doc["floors"]]
        self.names_floor = [F["name"] for F in self.doc["floors"]]
        self.NF = len(self.ids)
        self.floors = []
        self.ROOM_FLOOR = {}        # {(floor index, room name): the room's floor relative to the floor datum, m}
        self.kinds = {}             # {(floor index, room name): room kind}
        self.plates = {o["id"]: (o["plate"]["material"], float(o["plate"]["thickness"]))
                       for F in self.doc["floors"] for o in F.get("objects", []) if o.get("plate")}
        for fi, F in enumerate(self.doc["floors"]):
            fid = F["id"]
            base = os.path.join(self.build_dir, fid)
            fp = json.load(open(base + ".floorplan.json", encoding="utf-8"))
            rf = np.array(Image.open(base + ".rf.png"))
            rooms = np.array(Image.open(base + ".rooms.png"))
            id2name = {r["id"]: r["name"] for r in fp["rooms"]}
            id2out = {r["id"]: bool(r.get("outdoor")) for r in fp["rooms"]}
            idx2name = {int(k): id2name[v] for k, v in fp["raster_index"].items()}
            idx2id = {int(k): v for k, v in fp["raster_index"].items()}
            idx2out = {int(k): id2out[v] for k, v in fp["raster_index"].items()}
            for r in fp["rooms"]:
                self.kinds[(fi, r["name"])] = r["kind"]
                if r.get("floor_m"):
                    self.ROOM_FLOOR[(fi, r["name"])] = float(r["floor_m"])
            ymax = float(fp["floor"]["bounds"][1][1])
            L = np.load(base + ".layers.npz")
            lc, lz0, lz1 = L["cls"], L["zmin"], L["zmax"]
            hgt = lz1.max(axis=0).astype(np.float32)            # the single top: the highest layer's top
            A = np.load(base + ".apertures.npz")
            pmat = np.zeros(rf.shape, np.uint8); ptop = np.zeros(rf.shape, np.float32); pth = np.zeros(rf.shape, np.float32)
            for o in F.get("objects", []):
                if o["id"] not in self.plates:
                    continue
                mat, th = self.plates[o["id"]]
                pts = np.array([[(int((x - self.X0) / RES), int((ymax - y) / RES)) for x, y in o["outline"]]], np.int32)
                top = round(float(o["z_min"]) + float(o["height"]), 6)
                cv2.fillPoly(pmat, pts, PLATE_MATERIALS.index(mat) + 1)
                cv2.fillPoly(ptop, pts, top)
                cv2.fillPoly(pth, pts, th)
            self.floors.append(dict(id=fid, rf=rf, rooms=rooms, hgt=hgt, ymax=ymax, pmat=pmat, ptop=ptop, pth=pth,
                                    lc=lc, lz0=lz0, lz1=lz1, ac=A["code"], asill=A["sill"], ahead=A["head"],
                                    z=float(fp["floor"]["z"]), ceil=float(fp["floor"]["ceiling"]),
                                    names=idx2name, room_ids=idx2id, outdoor=idx2out))
        N = self.NF
        H = max(f["rf"].shape[0] for f in self.floors); W = max(f["rf"].shape[1] for f in self.floors)
        self.H, self.W = H, W
        self.RF = np.full((N, H, W), 255, np.uint8)
        self.HG = np.zeros((N, H, W), np.float32)
        self.PM = np.zeros((N, H, W), np.uint8); self.PT = np.zeros((N, H, W), np.float32)
        self.PTH = np.zeros((N, H, W), np.float32)
        self.LC = np.zeros((N, 4, H, W), np.uint8); self.LZ0 = np.zeros((N, 4, H, W), np.float32); self.LZ1 = self.LZ0.copy()
        self.AC = np.zeros((N, H, W), np.uint8); self.AS = np.zeros((N, H, W), np.float32); self.AH = self.AS.copy()
        for i, f in enumerate(self.floors):
            h, w = f["rf"].shape
            self.RF[i, :h, :w] = f["rf"]; self.HG[i, :h, :w] = f["hgt"]
            self.PM[i, :h, :w] = f["pmat"]; self.PT[i, :h, :w] = f["ptop"]; self.PTH[i, :h, :w] = f["pth"]
            self.LC[i, :, :h, :w] = f["lc"]; self.LZ0[i, :, :h, :w] = f["lz0"]; self.LZ1[i, :, :h, :w] = f["lz1"]
            self.AC[i, :h, :w] = f["ac"]; self.AS[i, :h, :w] = f["asill"]; self.AH[i, :h, :w] = f["ahead"]
        self.Z = np.array([f["z"] for f in self.floors])
        self.C = np.array([f["ceil"] for f in self.floors])
        self.YM = np.array([f["ymax"] for f in self.floors])
        # Stairs (house.json): a ramp in the upper stairs room's footprint (Nick 2026-09-26: a flight is a sloped
        # slab, the open air above it belongs to whichever floor the height says), portals at the foot and the top.
        self.RAMPS, self.PORTALS, self.stairs = [], {}, []
        for st in self.doc.get("stairs", []):
            lo, hi = self.floor_index(st["lower"]["floor"]), self.floor_index(st["upper"]["floor"])
            fu, fl = self.floors[hi], self.floors[lo]
            iu, il = self._room_index(hi, st["upper"]["room"]), self._room_index(lo, st["lower"]["room"])
            self.stairs.append(dict(lo=lo, hi=hi, lower_room=fl["names"][il], upper_room=fu["names"][iu]))
            self.PORTALS.setdefault((lo, hi), (tuple(st["foot"]), tuple(st["top"])))
            if not USE_RAMPS:
                continue
            rr, cc = np.nonzero(fu["rooms"] == iu)
            x0, x1 = self.X0 + cc.min() * RES, self.X0 + (cc.max() + 1) * RES
            y0, y1 = fu["ymax"] - (rr.max() + 1) * RES, fu["ymax"] - rr.min() * RES
            zb, zt = float(self.Z[lo]), float(self.Z[hi])
            if st.get("risers"):
                riser = (zt - zb) / sum(st["risers"])
                zb, zt = zb + st["risers"][0] * riser, zt - st["risers"][2] * riser
            self.RAMPS.append(Ramp(lo, x0, y0, x1, y1, st["run"]["axis"], zb, zt, st["run"]["from"], st["run"]["to"]))

    def _room_index(self, fi, room_id):
        return next(i for i, rid in self.floors[fi]["room_ids"].items() if rid == room_id)

    def floor_index(self, fid):
        return self.ids.index(fid)

    def room_kind(self, fi, name):
        return self.kinds.get((fi, name))

    # ---- lookups ------------------------------------------------------------------
    def floor_z(self, fi, room=None):
        """Absolute height of the floor a thing in `room` on floor fi stands on: the floor datum Z[fi], or lower
        for a sunken room (the garage, one riser down). Tape-measured heights are added to this."""
        return float(self.Z[fi]) + self.ROOM_FLOOR.get((fi, room), 0.0)

    def floor_of_z(self, z):
        return int(np.searchsorted(self.Z, z, side="right") - 1)

    def cell(self, fi, x, y):
        return int((self.YM[fi] - y) / RES), int((x - getattr(self, "X0", 0.0)) / RES)

    def room_at(self, fi, x, y):
        f = self.floors[fi]; r, c = self.cell(fi, x, y)
        if 0 <= r < f["rooms"].shape[0] and 0 <= c < f["rooms"].shape[1]:
            return f["names"].get(int(f["rooms"][r, c]))
        return None

    def wall_clearance(self, fi, x, y):
        f = self.floors[fi]
        clr = getattr(self, "_clr", None)
        if clr is None:                 # built whole, then published: devices are stepped in parallel threads, and a
            clr = [ndimage.distance_transform_edt(~np.isin(g["rf"], (4, 5, 6, 8, 255))) * RES   # half-built list
                   for g in self.floors]                                                       # was an IndexError
            self._clr = clr
        r, c = self.cell(fi, x, y)
        return float(clr[fi][r, c]) if 0 <= r < f["rf"].shape[0] and 0 <= c < f["rf"].shape[1] else 0.0

    def nudge(self, x, y, z, room=None):
        """Move a radio out of a wall cell into the nearest in-room cell.

        Outlet-mounted proxies are recorded at the wall line, which lands them inside the
        ~10 cm raster wall band. A steep ray then travels down inside the wall and gets
        charged the radio's own mounting wall. The antenna is really in the room.
        """
        fi = self.floor_of_z(z); f = self.floors[fi]
        # Constrain to the radio's own room: a proxy on a shared wall must not be nudged
        # into the neighbour (a proxy on the wall to a garage landed in the garage without this).
        if room is None:
            ok = (f["rooms"] > 0) & np.isin(f["rf"], (0, 1, 2, 3))
        else:
            idx = [k for k, v in f["names"].items() if v == room]
            if not idx:
                raise ValueError(f"room {room!r} not on floor {f['id']}")
            ok = np.isin(f["rooms"], idx) & np.isin(f["rf"], (0, 1, 2, 3))
        _, ind = ndimage.distance_transform_edt(~ok, return_indices=True)
        r, c = self.cell(fi, x, y)
        r = min(max(r, 0), ok.shape[0] - 1); c = min(max(c, 0), ok.shape[1] - 1)
        if ok[r, c]:
            return x, y, z, 0.0
        r2, c2 = int(ind[0][r, c]), int(ind[1][r, c])
        nx = getattr(self, "X0", 0.0) + c2 * RES + RES / 2; ny = f["ymax"] - (r2 * RES + RES / 2)
        return nx, ny, z, float(np.hypot(nx - x, ny - y))

    # ---- vectorised 3-D ray-march ------------------------------------------------
    def _wall_distance(self):
        """Per-floor distance (m) from each cell to the nearest wall-class cell (interior or
        exterior). Cached on the instance; used for Fresnel-zone grazing."""
        if not hasattr(self, "_WD"):
            from scipy.ndimage import distance_transform_edt
            self._WD = np.stack([distance_transform_edt(~np.isin(self.RF[i], (4, 5))) * RES
                                 for i in range(self.RF.shape[0])])
        return self._WD

    def _cls_at(self, fi, rr, cc, zrel, ok):
        """Material by height (spec 2026-09-26). Returns (wallcls, fixcls):
        wallcls - the wall/aperture class at this height: the aperture's code between its sill and head, else the
                  wall's own class (4/5), else 0;
        fixcls  - the fixture layer this height is inside (the densest where layers overlap), else 0. With no layers
                  built (an old build), falls back to the flat raster class gated by the single top (HG)."""
        base = np.where(ok, self.RF[fi, rr, cc], 0)
        # fi is a per-sample ARRAY here: never index a whole floor with it (self.LC[fi] fancy-indexes a copy of the
        # raster per sample - 89 GB for one chunk). Cheap scalar flags instead.
        has_bands = getattr(self, "_has_bands", None)
        if has_bands is None:
            has_bands = self._has_bands = bool(hasattr(self, "AC") and self.AC.any())
        if has_bands:
            ac = self.AC[fi, rr, cc]
            inband = ok & (ac > 0) & (zrel >= self.AS[fi, rr, cc]) & (zrel < self.AH[fi, rr, cc])
            wall = np.where(inband, ac, np.where((base >= 4) & (base <= 8), base, 0)).astype(np.int16)
        else:                                                    # old build or a bare test House: the flat raster
            wall = np.where((base >= 4) & (base <= 8), base, 0).astype(np.int16)
        fix = np.zeros_like(wall)
        has_layers = getattr(self, "_has_layers", None)
        if has_layers is None:
            has_layers = self._has_layers = bool(hasattr(self, "LC") and self.LC.any())
        if has_layers:
            for k in range(self.LC.shape[1]):
                lc = self.LC[fi, k, rr, cc].astype(np.int16)
                hit = ok & (lc > 0) & (zrel >= self.LZ0[fi, k, rr, cc]) & (zrel < self.LZ1[fi, k, rr, cc])
                fix = np.where(hit & (lc > fix), lc, fix)
        else:
            taller = self.HG[fi, rr, cc] > zrel
            fix = np.where(ok & taller & ((base <= 3) | (base == 9)), base, 0).astype(np.int16)
        return wall, fix

    def _exact_walls(self, sx, sy, sz, P, chunk=400):
        """Exact per-cell traversal (RP_RAYS=exact) for the wall and fixture features.

        The sampled march charges a wall by how many of its SAMPLES points land in wall cells, so a
        ray that clips a cell corner is charged a whole sample (or nothing), and every crossing is
        off by up to one step. Here each ray is cut at every cell boundary (x lines, y lines of each
        floor raster), every floor/ceiling height and the two guard points; each piece lies in exactly
        one cell, and its length x d3 is the metres of that cell's material actually traversed.
        Crossings (k*) count runs of consecutive same-class pieces, as before.
        """
        P = np.asarray(P, float)
        keys = ("k4", "k5", "k6", "k7", "k8", "m4", "m5", "m6", "m7", "m8", "klow", "kmed", "kh", "mh", "kwat", "mwat",
                "mpw", "mps", "mpm", "kpw", "kps", "kpm")
        PM = getattr(self, "PM", None)
        out = {k: np.zeros(len(P)) for k in keys}
        zb = np.concatenate([self.Z, self.Z + self.C])
        yms = np.unique(self.YM)
        for a in range(0, len(P), chunk):
            Q = P[a:a + chunk]
            n = len(Q)
            dx, dy, dz = Q[:, 0] - sx, Q[:, 1] - sy, Q[:, 2] - sz
            d3 = np.sqrt(dx * dx + dy * dy + dz * dz)
            cuts = [np.zeros((n, 1)), np.ones((n, 1))]

            def grid(s, d, lines_origin, sign):
                # boundaries at origin + sign * k * RES between s and s + d
                lo = np.minimum(s, s + d); hi = np.maximum(s, s + d)
                klo = np.ceil((sign * (lo - lines_origin)) / RES - 1e-9) if sign > 0 else np.ceil((lines_origin - hi) / RES - 1e-9)
                khi = np.floor((sign * (hi - lines_origin)) / RES + 1e-9) if sign > 0 else np.floor((lines_origin - lo) / RES + 1e-9)
                m = int(np.max(khi - klo + 1, initial=0))
                if m <= 0:
                    return None
                ks = klo[:, None] + np.arange(m)[None, :]
                pos = lines_origin + sign * ks * RES
                with np.errstate(divide="ignore", invalid="ignore"):
                    t = (pos - s[:, None]) / d[:, None]
                t[(ks > khi[:, None]) | (d[:, None] == 0)] = np.nan
                return t

            tx = grid(np.full(n, sx), dx, getattr(self, "X0", 0.0), +1)
            if tx is not None:
                cuts.append(tx)
            for ym in yms:
                ty = grid(np.full(n, sy), dy, ym, -1)
                if ty is not None:
                    cuts.append(ty)
            with np.errstate(divide="ignore", invalid="ignore"):
                tz = (zb[None, :] - sz) / dz[:, None]
            tz = np.where(dz[:, None] == 0, np.nan, tz)
            cuts.append(tz)
            with np.errstate(divide="ignore", invalid="ignore"):
                cuts.append((GUARD_SRC / d3)[:, None]); cuts.append((1.0 - GUARD_DST / d3)[:, None])
            T = np.concatenate(cuts, axis=1)
            T[(T < 0) | (T > 1)] = np.nan
            T = np.sort(T, axis=1)                                   # NaN sort to the end
            t0, t1 = T[:, :-1], T[:, 1:]
            L = np.nan_to_num(t1 - t0, nan=0.0)
            valid = L > 1e-12
            mid = np.where(valid, 0.5 * (t0 + t1), 0.0)
            X = sx + dx[:, None] * mid; Y = sy + dy[:, None] * mid; Z = sz + dz[:, None] * mid
            fi = np.clip(np.searchsorted(self.Z, Z, side="right") - 1, 0, len(self.Z) - 1)
            in_floor = (Z >= self.Z[fi]) & (Z < self.Z[fi] + self.C[fi])
            r = np.floor((self.YM[fi] - Y) / RES).astype(int); c = np.floor((X - getattr(self, "X0", 0.0)) / RES).astype(int)
            ok = valid & in_floor & (r >= 0) & (r < self.H) & (c >= 0) & (c < self.W)
            rr = np.clip(r, 0, self.H - 1); cc = np.clip(c, 0, self.W - 1)
            cls, fix = self._cls_at(fi, rr, cc, Z - self.Z[fi], ok)
            live = (mid * d3[:, None] >= GUARD_SRC - 1e-12) & ((1.0 - mid) * d3[:, None] >= GUARD_DST - 1e-12)
            metres = L * d3[:, None]
            # index of the previous VALID piece, so zero-length pieces at cell corners never split a run
            idx = np.where(valid, np.arange(valid.shape[1])[None, :], -1)
            last = np.maximum.accumulate(idx, axis=1)
            prev = np.concatenate([np.full((n, 1), -1), last[:, :-1]], axis=1)

            def runs(sel):
                ps = np.take_along_axis(sel, np.clip(prev, 0, None), axis=1) & (prev >= 0)
                return (sel & ~ps).sum(1)

            for code, key in ((4, "4"), (5, "5"), (6, "6"), (7, "7"), (8, "8")):
                sel = ok & live & (cls == code)
                out["k" + key][a:a + n] = runs(sel)
                out["m" + key][a:a + n] = (metres * sel).sum(1)
            for code, key in ((1, "klow"), (2, "kmed"), (3, "kh"), (RF_CODE["water"], "kwat")):
                sel = ok & live & (fix == code)
                out[key][a:a + n] = runs(sel)
                if key in ("kh", "kwat"):
                    out["mh" if key == "kh" else "mwat"][a:a + n] = (metres * sel).sum(1)
            # thin tops and sheets (the plates, now layers): metres inside a wood / stone / metal-top layer, capped per
            # crossing at PLATE_CAP x the layer's thickness as the old plate raster was (a shallow path runs far
            # inside a 3 cm top; real signal goes over the edge)
            if fix.any() and getattr(self, "_has_layers", False):          # layers only (a bare House has no thicknesses)
                thick = np.zeros(fix.shape, np.float32)
                for k in range(self.LC.shape[1]):
                    lc = self.LC[fi, k, rr, cc]
                    hit = ok & (lc == fix) & (lc > 0)
                    thick = np.where(hit, self.LZ1[fi, k, rr, cc] - self.LZ0[fi, k, rr, cc], thick)
                for code, key in ((RF_CODE["wood"], "pw"), (RF_CODE["stone"], "ps"), (RF_CODE["metal_top"], "pm")):
                    sel = ok & live & (fix == code)
                    kcnt = runs(sel)
                    mtot = (metres * sel).sum(1)
                    if PLATE_CAP > 0:
                        thw = np.where(mtot > 0, (thick * metres * sel).sum(1) / np.maximum(mtot, 1e-12), 0.0)
                        mtot = np.minimum(mtot, kcnt * PLATE_CAP * thw)
                    out["k" + key][a:a + n] = out["k" + key][a:a + n] + kcnt
                    out["m" + key][a:a + n] = out["m" + key][a:a + n] + mtot
            if PM is not None and not getattr(self, "_has_layers", False):
                # OLD plate raster (superseded by the layers; only for a build without them):
                # the part of each piece whose height lies in [top - thickness, top]
                pm = np.where(ok & live, PM[fi, rr, cc], 0)
                top = self.PT[fi, rr, cc] + self.Z[fi]; th = self.PTH[fi, rr, cc]
                with np.errstate(divide="ignore", invalid="ignore"):
                    ta = (top - th - sz) / dz[:, None]; tb = (top - sz) / dz[:, None]
                lo = np.maximum(np.nan_to_num(t0, nan=0.0), np.minimum(ta, tb))
                hi = np.minimum(np.nan_to_num(t1, nan=0.0), np.maximum(ta, tb))
                frac = np.clip(hi - lo, 0.0, None)
                flat = dz[:, None] == 0
                inband = (Z >= top - th) & (Z <= top)
                frac = np.where(flat, np.where(inband, L, 0.0), frac)
                pmetres = np.where(pm > 0, frac * d3[:, None], 0.0)
                for code, key in ((1, "w"), (2, "s"), (3, "m")):
                    sel = (pm == code) & (pmetres > 1e-12)
                    mtot = (pmetres * sel).sum(1)
                    kcnt = runs(sel)
                    if PLATE_CAP > 0:
                        thw = np.where(mtot > 0, (th * pmetres * sel).sum(1) / np.maximum(mtot, 1e-12), 0.0)
                        mtot = np.minimum(mtot, kcnt * PLATE_CAP * thw)
                    out["mp" + key][a:a + n] = mtot
                    out["kp" + key][a:a + n] = kcnt
        return out

    def rays(self, src, pts, chunk=1500):
        """src (x,y,z_abs); pts (N,3). Returns dict of (N,) feature arrays."""
        pts = np.asarray(pts, float)
        sx, sy, sz = src
        fsrc = self.floor_of_z(sz)
        out = {k: np.zeros(len(pts)) for k in ("d3", "dxy", "dz", "k4", "k5", "k6", "k7", "k8",
                                               "klow", "kmed", "kh", "kwat", "mwat", "kslab",
                                               "m4", "m5", "m6", "m7", "m8", "mh", "mgraze", "mslab", "ax", "ay")}
        WD = self._wall_distance()
        LAMBDA = 0.125                           # 2.4 GHz
        t = (np.arange(1, SAMPLES) / SAMPLES)[None, :]
        for a in range(0, len(pts), chunk):
            P = pts[a:a + chunk]
            dx, dy, dz = P[:, 0] - sx, P[:, 1] - sy, P[:, 2] - sz
            d3 = np.sqrt(dx * dx + dy * dy + dz * dz)
            X = sx + dx[:, None] * t; Y = sy + dy[:, None] * t; Z = sz + dz[:, None] * t
            fi = np.clip(np.searchsorted(self.Z, Z, side="right") - 1, 0, len(self.Z) - 1)
            in_floor = (Z >= self.Z[fi]) & (Z < self.Z[fi] + self.C[fi])
            r = ((self.YM[fi] - Y) / RES).astype(int); c = ((X - getattr(self, "X0", 0.0)) / RES).astype(int)
            ok = in_floor & (r >= 0) & (r < self.H) & (c >= 0) & (c < self.W)
            rr = np.clip(r, 0, self.H - 1); cc = np.clip(c, 0, self.W - 1)
            zrel = Z - self.Z[fi]
            cls, fix = self._cls_at(fi, rr, cc, zrel, ok)     # wall/aperture class and fixture layer at this height
            low_f = ok & (fix == 1)
            med_f = ok & (fix == 2)
            high = ok & (fix == 3)
            water = ok & (fix == RF_CODE["water"])
            along = d3[:, None] * t
            live = (along >= GUARD_SRC) & ((d3[:, None] - along) >= GUARD_DST)
            step = d3 / SAMPLES                      # metres represented by one sample on each ray
            for code, key in ((4, "k4"), (5, "k5"), (6, "k6"), (7, "k7"), (8, "k8")):
                st = (cls == code) & live
                out[key][a:a + chunk] = st[:, 0] + (st[:, 1:] & ~st[:, :-1]).sum(1)
                # metres of this material actually traversed: thickness x 1/cos(incidence)
                out["m" + key[1:]][a:a + chunk] = st.sum(1) * step
            # Fresnel grazing: samples OUTSIDE any wall whose first-Fresnel-zone radius reaches
            # into one. r_F = sqrt(lambda * s(1-s) * d3), s = fractional position along the ray.
            sfrac = t                                                   # (1, SAMPLES-1)
            rF = np.sqrt(LAMBDA * sfrac * (1.0 - sfrac) * d3[:, None])  # (chunk, SAMPLES-1)
            wd = np.where(ok, WD[fi, rr, cc], 99.0)
            inwall = (cls == 4) | (cls == 5)
            w = np.clip(1.0 - wd / np.maximum(rF, 1e-3), 0.0, 1.0) * (~inwall) * live
            out["mgraze"][a:a + chunk] = w.sum(1) * step
            for arr, key in ((low_f, "klow"), (med_f, "kmed"), (high, "kh"), (water, "kwat")):
                hs = arr & live
                out[key][a:a + chunk] = hs[:, 0] + (hs[:, 1:] & ~hs[:, :-1]).sum(1)
                if key in ("kh", "kwat"):
                    out["mh" if key == "kh" else "mwat"][a:a + chunk] = hs.sum(1) * step
            ks = self._slab_crossings(src, P, X, Y, Z)
            out["kslab"][a:a + chunk] = ks
            # metres of assembly traversed: thickness / cos(angle from vertical), capped at 3x
            cosv = np.abs(dz) / np.maximum(d3, 1e-6)
            out["mslab"][a:a + chunk] = ks * ASSEMBLY / np.maximum(cosv, 1.0 / 3.0)
            out["d3"][a:a + chunk] = d3
            out["dxy"][a:a + chunk] = np.hypot(dx, dy)
            out["dz"][a:a + chunk] = np.abs(dz)
            dxy = np.maximum(np.hypot(dx, dy), 1e-6)                 # unit bearing receiver -> point (RP_AZGAIN)
            out["ax"][a:a + chunk] = dx / dxy; out["ay"][a:a + chunk] = dy / dxy
        if EXACT:
            out.update(self._exact_walls(sx, sy, sz, pts))
        return out

    def _slab_crossings(self, src, P, X, Y, Z):
        """Floor/ceiling assemblies crossed per ray: for each pair of floors, sign changes of (z - surface)
        between consecutive samples (end points included), where the surface is the flight inside a stair
        well and the flat slab elsewhere - decided per step by its midpoint. A straight ray outside every
        well crosses each slab at most once, so this equals the old |floor(dst) - floor(src)| there."""
        sx, sy, sz = src
        n = len(P)
        Xe = np.concatenate([np.full((n, 1), sx), X, P[:, :1]], axis=1)
        Ye = np.concatenate([np.full((n, 1), sy), Y, P[:, 1:2]], axis=1)
        Ze = np.concatenate([np.full((n, 1), sz), Z, P[:, 2:3]], axis=1)
        xm, ym = 0.5 * (Xe[:, 1:] + Xe[:, :-1]), 0.5 * (Ye[:, 1:] + Ye[:, :-1])
        ramps = {r.lower: r for r in getattr(self, "RAMPS", [])}
        ks = np.zeros(n)
        for lo in range(len(self.Z) - 1):
            plane = Ze < self.Z[lo + 1]
            cross = plane[:, 1:] != plane[:, :-1]
            r = ramps.get(lo)
            if r is not None:
                # a device lying on a tread sits a hair under the ramp (float noise, the nosing): on it, not under it
                under = Ze < r.surface(Xe, Ye) - RAMP_EPS
                cross = np.where(r.inside(xm, ym), under[:, 1:] != under[:, :-1], cross)
            ks += cross.sum(1)
        return ks

    # ---- door-routed geodesics ---------------------------------------------------
    # PORTALS (set in __init__ from house.json stairs): (lower, upper) -> (foot xy on lower floor, top xy on upper).

    def portal_xy(self, fi, toward):
        """The stair portal on floor fi you take to travel toward floor `toward`."""
        lo, hi = (fi, toward) if toward > fi else (toward, fi)
        a, b = self.PORTALS[(lo, hi)]
        return a if fi == lo else b

    def flight_len(self, lo, hi):
        a, b = self.PORTALS[(lo, hi)]
        return float(np.hypot(np.hypot(b[0] - a[0], b[1] - a[1]), self.Z[hi] - self.Z[lo]))

    def portal_field(self, fi, from_floor):
        """Geodesic field on floor fi from the portal you arrive at when coming from from_floor."""
        if not hasattr(self, "_pfields"):
            self._pfields = {}
        key = (fi, from_floor)
        if key not in self._pfields:
            x, y = self.portal_xy(fi, from_floor)
            self._pfields[key] = self.geodesic_field(fi, x, y)
        return self._pfields[key]

    def stair_route_head(self, fs, fi, sx, sy):
        """Routed length from (sx, sy) on floor fs to the ARRIVAL portal on floor fi, via the
        stairs (through the main floor when going floor 1 <-> floor 3). inf if unreachable."""
        step = 1 if fi > fs else -1
        if any((min(c, c + step), max(c, c + step)) not in self.PORTALS for c in range(fs, fi, step)):
            return float("inf")                     # no stairs between two of the floors on the way
        src_field = self.geodesic_field(fs, sx, sy)
        total, cur = 0.0, fs
        field = src_field
        while cur != fi:
            nxt = cur + step
            px, py = self.portal_xy(cur, nxt)
            r, c = self.cell(cur, px, py)
            r = min(max(r, 0), field.shape[0] - 1); c = min(max(c, 0), field.shape[1] - 1)
            total += float(field[r, c]) + self.flight_len(min(cur, nxt), max(cur, nxt))
            field = self.portal_field(nxt, cur)      # continue from the arrival portal on nxt
            cur = nxt
        return total

    def geodesic_field(self, fi, x, y):
        """Shortest path through free space and doorways on floor fi from (x,y)."""
        f = self.floors[fi]
        if not hasattr(self, "_graphs"):
            self._graphs = {}
        if fi not in self._graphs:
            pas = np.isin(f["rf"], PASSABLE)
            h, w = pas.shape
            idx = -np.ones((h, w), int); idx[pas] = np.arange(pas.sum())
            rows, cols, wts = [], [], []
            for dr, dc, wt in ((0, 1, RES), (1, 0, RES), (1, 1, RES * 2 ** 0.5), (1, -1, RES * 2 ** 0.5)):
                c0, c1 = max(0, -dc), w - max(0, dc)          # columns of the left cell
                A = pas[0:h - dr, c0:c1];  B = pas[dr:h, c0 + dc:c1 + dc]
                IA = idx[0:h - dr, c0:c1]; IB = idx[dr:h, c0 + dc:c1 + dc]
                m = A & B
                rows.append(IA[m]); cols.append(IB[m]); wts.append(np.full(int(m.sum()), wt))
            n = int(pas.sum())
            G = coo_matrix((np.concatenate(wts), (np.concatenate(rows), np.concatenate(cols))), shape=(n, n)).tocsr()
            _, near = ndimage.distance_transform_edt(~pas, return_indices=True)
            self._graphs[fi] = (G, idx, near, pas)
        G, idx, near, pas = self._graphs[fi]
        r, c = self.cell(fi, x, y)
        r = min(max(r, 0), pas.shape[0] - 1); c = min(max(c, 0), pas.shape[1] - 1)
        if not pas[r, c]:
            r, c = near[0][r, c], near[1][r, c]
        dist = dijkstra(G, directed=False, indices=int(idx[r, c]))
        field = np.full(pas.shape, np.inf); field[pas] = dist
        return field

    # ---- candidate locations -----------------------------------------------------
    def candidates(self, stride=5, heights=(0.3, 0.9, 1.5)):
        P, meta = [], []
        for fi, f in enumerate(self.floors):
            h, w = f["rf"].shape
            for r in range(stride // 2, h, stride):
                for c in range(stride // 2, w, stride):
                    if f["rooms"][r, c] == 0 or f["rf"][r, c] not in (0, 1, 2, 3):
                        continue
                    x = self.X0 + c * RES + RES / 2; y = f["ymax"] - (r * RES + RES / 2)
                    nm = f["names"].get(int(f["rooms"][r, c]))
                    for hh in heights:
                        P.append((x, y, f["z"] + hh)); meta.append((fi, nm, hh, r, c))
        return np.array(P), meta


def move_timeline(v):
    """[(iso, position_before), ...] oldest first. `moved_at` + `previous` are one stamp and one position
    (a single move) or equal-length lists: previous[i] is where the scanner stood before moved_at[i]."""
    at, prev = v.get("moved_at"), v.get("previous")
    if not at or not prev:
        return []
    at = [at] if isinstance(at, str) else list(at)
    prev = [prev] if isinstance(prev, dict) else list(prev)
    if len(at) != len(prev):
        raise ValueError(f"moved_at has {len(at)} stamps but previous has {len(prev)} positions")
    return sorted(zip(at, prev), key=lambda p: datetime.fromisoformat(p[0]).timestamp())


def load_scanners(historical=False, doc=None):
    """Scanner positions. `historical=True` returns where each scanner stood before its FIRST recorded
    move (`moved_at` / `previous`, see move_timeline).

    A scanner that moves invalidates the geometry of every capture taken before the move. The fit
    featurises each capture at the position in effect when it was taken (bakeoff.Data) and live
    localisation runs on the current positions.
    """
    S = {k: v for k, v in _registry(doc).items() if v.get("x") is not None}
    if not historical:
        return S
    out = {}
    for k, v in S.items():
        mv = move_timeline(v)
        out[k] = {**v, **mv[0][1]} if mv else v
    return out


def move_timelines(doc=None):
    """{scanner: move_timeline} for every scanner that has moved."""
    S = _registry(doc)
    return {k: mv for k, v in S.items() if v.get("x") is not None for mv in [move_timeline(v)] if mv}


def moved_scanners(doc=None):
    """{scanner: [move stamps, oldest first]}."""
    return {k: [w for w, _ in mv] for k, mv in move_timelines(doc).items()}


def as_epochs(v):
    """`replaced_at` as a sorted list: one ISO time (older files) or several (a scanner re-seated,
    re-oriented or swapped more than once)."""
    if not v:
        return []
    return sorted([v] if isinstance(v, str) else list(v))


def _registry(doc=None):
    """The receivers as the model reads them: the house document's (spec 2026-10-01), else scanners_v2.json."""
    import house_receivers as HR
    return HR.registry(doc)


def added_scanners():
    """{scanner: epoch seconds it was installed} for scanners with an `added_at` stamp (ISO with a time; a bare
    date means midnight local). Before that moment the scanner cannot have heard anything, so a capture from
    then must not count it as a non-detection (bakeoff.absent_at)."""
    out = {}
    for k, v in _registry().items():
        if v.get("added_at"):
            out[k] = datetime.fromisoformat(v["added_at"]).timestamp()
    return out


def replaced_scanners():
    """Scanners whose HARDWARE was swapped in place (`replaced_at`). Same position, new receiver:
    the per-scanner level K belongs to the radio, so captures before the swap get their own
    '<name> (oldN)' K in the fit and the live scanner's K comes only from data after its last change."""
    S = _registry()
    skip = [x for x in os.environ.get("RP_NO_REPLACED", "").split(",") if x]
    return {k: as_epochs(v["replaced_at"]) for k, v in S.items() if v.get("replaced_at") and k not in skip}


def feature_bank(house, scanners, pts):
    """Features for every scanner against every point, plus the routed distance."""
    names = list(scanners)
    bank = {}
    for s in names:
        v = scanners[s]
        src = (v["x"], v["y"], v["z_abs"])
        src = house.nudge(*src, room=v.get("room"))[:3]
        feats = house.rays(src, pts)
        G = np.full(len(pts), np.inf)
        fis = np.clip(np.searchsorted(house.Z, pts[:, 2], side="right") - 1, 0, len(house.Z) - 1)
        for fi in np.unique(fis):
            field = house.geodesic_field(int(fi), v["x"], v["y"])
            sel = fis == fi
            rr = ((house.YM[fi] - pts[sel, 1]) / RES).astype(int)
            cc = ((pts[sel, 0] - getattr(house, "X0", 0.0)) / RES).astype(int)
            rr = np.clip(rr, 0, field.shape[0] - 1); cc = np.clip(cc, 0, field.shape[1] - 1)
            G[sel] = field[rr, cc]
        feats["G"] = G
        # Cross-floor route via the open stairwell: head (src -> arrival portal on the target
        # floor) plus the geodesic from that portal to each point.
        Gx = np.full(len(pts), np.inf)
        fs = house.floor_of_z(src[2])
        for fi in np.unique(fis):
            fi = int(fi)
            if fi == fs:
                continue
            sel = fis == fi
            head = house.stair_route_head(fs, fi, v["x"], v["y"])
            if not np.isfinite(head):
                continue
            field = house.portal_field(fi, fi + 1 if fi < fs else fi - 1)   # the floor you arrive from
            rr = ((house.YM[fi] - pts[sel, 1]) / RES).astype(int)
            cc = ((pts[sel, 0] - getattr(house, "X0", 0.0)) / RES).astype(int)
            rr = np.clip(rr, 0, field.shape[0] - 1); cc = np.clip(cc, 0, field.shape[1] - 1)
            Gx[sel] = head + field[rr, cc]
        feats["Gx"] = Gx
        # 8-connected grid geodesics overstate straight lines (octile metric, up to +8%).
        # Detour is measured against the octile length of the straight path, not dxy.
        ax_, ay_ = np.abs(pts[:, 0] - src[0]), np.abs(pts[:, 1] - src[1])
        feats["oct"] = np.maximum(ax_, ay_) + (2 ** 0.5 - 1) * np.minimum(ax_, ay_)
        bank[s] = feats
    return names, bank


def bank_key(house, scanners, pts):
    """Cache key for a feature bank. Keys on the GEOMETRY too: a raster, fixture height range or window sill/head
    change that leaves the candidate set unchanged would otherwise silently reuse stale features."""
    # the scanners by what feature_bank reads of them (not their notes or bookkeeping, 2026-10-01: receivers moved
    # into the house document, which records the same positions with other fields)
    geo = {k: {f: v.get(f) for f in ("x", "y", "z_abs", "room")} for k, v in scanners.items()}
    h = hashlib.sha1(f"featv10|g{GUARD_SRC}|r{int(USE_RAMPS)}|{'x' + json.dumps(getattr(house, "plates", {}), sort_keys=True) + f'c{PLATE_CAP}' if EXACT else ''}".encode() + json.dumps(geo, sort_keys=True).encode() + pts.tobytes())
    for f in house.floors:
        for k in ("rf", "hgt", "lc", "lz0", "lz1", "ac", "asill", "ahead"):
            if k in f:
                h.update(np.ascontiguousarray(f[k]).tobytes())
    return h.hexdigest()[:12]


def cached_bank(house, scanners, pts, tag):
    name = f".bank_{tag}_{bank_key(house, scanners, pts)}.npz"
    for d in dict.fromkeys((cache_dir(), HERE)):          # the data folder's, then the one baked into the image
        fn = os.path.join(d, name)
        if os.path.exists(fn):
            z = np.load(fn, allow_pickle=True)
            if d == cache_dir():
                try:
                    os.utime(fn, None)                         # in use: an apply's pruning keeps what was read
                except OSError:
                    pass
            return list(z["names"]), z["bank"].item()
    names, bank = feature_bank(house, scanners, pts)
    d = cache_dir()
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f"{name}.{os.getpid()}.part")  # whole or not at all: an apply's dry run may write it too
    with open(tmp, "wb") as f:
        np.savez(f, names=np.array(names), bank=np.array(bank, dtype=object))
    os.replace(tmp, os.path.join(d, name))
    return names, bank
