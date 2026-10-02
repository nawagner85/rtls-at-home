"""Localisation engine for the live viewer.

Runs the frozen v3 models from ../../solver exactly as they were scored, but precomputes
every scanner's predicted RSSI at every candidate point once at startup, so a live update is
only the likelihood step. `Engine.parity()` proves the output matches solver/models.localise.

Also carries an optional room-level HMM filter: you can only move to an adjacent room, and
the only vertical links are the stairs. Off by default in the UI; raw model output is primary.
"""
import hashlib
import math
import os
import sys
import time

import json
import numpy as np
from scipy import ndimage
from scipy.special import log_ndtr

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SOLVER = os.path.join(ROOT, "solver")
sys.path.insert(0, SOLVER)
import bakeoff as B          # noqa: E402
import calib_loader as CL    # noqa: E402
import house_doc as HD       # noqa: E402
import models as M           # noqa: E402

MODELS = ["FreeSpace", "DominantPath"]
LIVE_MIN_N = 3               # live windows are short; v3's min_n=10 was for 180 s captures

# The live posterior's arithmetic (Nick 2026-09-30: the engine will run on a Raspberry Pi). The exact math - float64,
# the transmit offset integrated on a 0.5 dB grid over +-2.5 prior sd, scipy's log-CDF for unheard receivers - cost
# 229 ms per device-step on one i9 NUC core (470 ms on a Pi 5), 99% of the live loop. float32, 2 dB steps capped
# at +-20 dB around the prior's centre and a log-CDF table cost 20 ms and called the same room and floor on all 240 real
# windows tried (09-28 session; position within 0.35 m in 95%). The offset likelihood is smooth and at least ~1.3 dB
# wide, so a 2 dB Riemann sum of it is near exact. RP_EXACT_POSTERIOR=1 restores the exact math (Engine.parity uses it).
EXACT_POSTERIOR = os.environ.get("RP_EXACT_POSTERIOR", "0") == "1"
FAST_OFF_STEP, FAST_OFF_SPAN = 2.0, 20.0
_ZT0, _ZT1, _ZST = -40.0, 10.0, 0.01
_ZTAB = log_ndtr(np.arange(_ZT0, _ZT1 + _ZST / 2, _ZST)).astype(np.float32)
_ZSLOPE = np.append(np.diff(_ZTAB), np.float32(0.0)).astype(np.float32)


def log_ndtr_fast(z):
    """log Phi(z) by linear interpolation in a 0.01-step table over [-40, 10] (error < 1e-4 nats), clipped outside:
    an unheard receiver's term for float32 arrays, ~2x cheaper than scipy's."""
    u = (np.clip(z, _ZT0, _ZT1) - np.float32(_ZT0)) * np.float32(1.0 / _ZST)
    i = np.minimum(u.astype(np.int32), len(_ZTAB) - 1)
    return _ZTAB[i] + (u - i) * _ZSLOPE[i]


def floor_name(house, fi):
    """The floor's display name (house.json; what Home Assistant shows)."""
    return house.names_floor[fi]


def code_hashes(house):
    out = {}
    for f in ("geom3d.py", "models.py", "bakeoff.py", "house_build.py"):
        out[f] = hashlib.sha1(open(os.path.join(SOLVER, f), "rb").read()).hexdigest()[:10]
    out["house"] = HD.doc_hash(house.doc)[:10]
    for fid in house.ids:
        p = os.path.join(house.build_dir, fid + ".rf.png")
        out[fid + ".rf.png"] = hashlib.sha1(open(p, "rb").read()).hexdigest()[:10]
    return out


def room_graph(house):
    """(floor_idx, room) -> set of neighbours. Doorways/openings only; stairs link floors.
    Same construction verified against Nick's floor plan on 2026-09-22 (25 edges + stairs)."""
    nb = {}
    for fi, f in enumerate(house.floors):
        pas = np.isin(f["rf"], (0, 1, 2, 3, 7))
        for idx, nm in f["names"].items():
            m = f["rooms"] == idx
            for _ in range(4):
                m = ndimage.binary_dilation(m) & pas
            for j in np.unique(f["rooms"][m]):
                j = int(j)
                if j and j != idx:
                    nb.setdefault((fi, nm), set()).add((fi, f["names"][j]))
                    nb.setdefault((fi, f["names"][j]), set()).add((fi, nm))
    for st in house.stairs:                       # the stairs link floors (house.json)
        a, b = (st["lo"], st["lower_room"]), (st["hi"], st["upper_room"])
        nb.setdefault(a, set()).add(b)
        nb.setdefault(b, set()).add(a)
    return nb


# Room weights (Nick 2026-09-26): how likely a room is to be ENTERED, applied to where belief spreads and to the
# leak. Outdoor rooms 0.25 (the patio has no receiver and, once believed, was never left), closets and other
# places nothing lives 0.5, everything else 1.0. RP_ROOM_W_OUT / RP_ROOM_W_CLOSET override (flags.py).
ROOM_W_OUT = float(os.environ.get("RP_ROOM_W_OUT", "0.25"))
ROOM_W_CLOSET = float(os.environ.get("RP_ROOM_W_CLOSET", "0.5"))


def room_weights(rooms, outdoor, closets):
    """rooms [(floor, name)], outdoor and closets {(floor, name)} (room kinds in house.json) -> weight per room."""
    w = []
    for r in rooms:
        if r in outdoor:
            w.append(ROOM_W_OUT)
        elif r in closets:
            w.append(ROOM_W_CLOSET)
        else:
            w.append(1.0)
    return np.array(w)


def cell_prior(cell_ridx, room_w):
    """Each candidate cell's prior weight: its room's weight (outdoor 0.25, closets 0.5, else 1)."""
    return np.asarray(room_w, float)[np.asarray(cell_ridx)]


def apply_prior(post, prior):
    p = np.asarray(post, float) * prior
    s = p.sum()
    return p / s if s > 0 else np.asarray(post, float)


class RoomFilter:
    """Forward filter over rooms. Stay with prob exp(-dt/tau), else move to a neighbour (weighted by how likely
    it is to be entered); a small epsilon lets strong evidence override the graph (hard zeros lock in mistakes)."""

    def __init__(self, rooms, graph, tau=60.0, eps=0.002, weights=None):
        self.rooms = rooms
        self.n = len(rooms)
        self.tau, self.eps = tau, eps
        self.w = np.ones(self.n) if weights is None else np.asarray(weights, float)
        idx = {r: i for i, r in enumerate(rooms)}
        A = np.zeros((self.n, self.n))
        for r, i in idx.items():
            nbrs = [idx[x] for x in graph.get(r, ()) if x in idx]
            if nbrs:
                ww = self.w[nbrs]
                A[i, nbrs] = ww / ww.sum() if ww.sum() > 0 else 1.0 / len(nbrs)
        self.A = A
        self.leak = self.w / self.w.sum()
        self.reset()

    def reset(self):
        self.b = np.full(self.n, 1.0 / self.n)
        self.ticks = 0          # a fresh filter seeds on its first evidence instead of walking from flat
        self.disagree = 0       # consecutive ticks the evidence confidently named a room the filter does not believe

    def fresh(self):
        """A new filter (own belief) sharing this one's transition matrix."""
        f = object.__new__(RoomFilter)
        f.rooms, f.n, f.tau, f.eps, f.A, f.w, f.leak = self.rooms, self.n, self.tau, self.eps, self.A, self.w, self.leak
        f.reset()
        return f

    def step(self, room_like, dt, beta, tau=None):
        # the room weights enter as a prior at the evidence's own tempo (beta tempers overlapping windows), so an
        # outdoor or closet room needs real evidence, accumulated over about one window, to be believed
        lik = np.power(np.maximum(room_like * self.w, 1e-300), beta)
        if self.ticks == 0:                                       # seed on the first window, not on a flat belief
            self.b = lik / lik.sum()
            self.ticks = 1
            return self.b
        stay = math.exp(-max(dt, 0.0) / (tau or self.tau))
        b = stay * self.b + (1.0 - stay) * (self.b @ self.A)
        b = (1.0 - self.eps) * b + self.eps * self.leak
        b = b * lik
        s = b.sum()
        self.b = b / s if s > 0 and np.isfinite(s) else np.full(self.n, 1.0 / self.n)
        self.ticks += 1
        # the escape hatch (Nick 2026-09-26: a real move the graph cannot walk hop by hop - upstairs from the garage):
        # when the evidence confidently names a room the filter does not believe for TELEPORT_TICKS in a row, re-seed
        raw = int(np.argmax(room_like))
        if raw != int(np.argmax(self.b)) and room_like[raw] / max(room_like.sum(), 1e-300) >= 0.5:
            self.disagree += 1
            if self.disagree >= TELEPORT_TICKS:
                self.b = lik / lik.sum()
                self.disagree = 0
        else:
            self.disagree = 0
        return self.b


TELEPORT_TICKS = 12                   # ~25 s of confident disagreement at a 2 s tick
# The filter's time constant by device kind (Nick 2026-09-26): the things that move (phones, pets) 15 s, tags that
# sit (lamps, locks, beacons) 30 s - halved the same day: "we move 2-3 m/s in the house; by the time we get
# across the first wall we're stuck on the second". RP_TAU_MOBILE / RP_TAU_TAG override (flags.py).
TAU_MOBILE = float(os.environ.get("RP_TAU_MOBILE", "15"))
TAU_TAG = float(os.environ.get("RP_TAU_TAG", "30"))


def filter_tau(kind):
    return TAU_MOBILE if kind in ("phone", "pet") else TAU_TAG


WARMUP_TICKS = 30                     # ~60 s at a 2 s tick: no CALL until the filter has history


class MotionFilter:
    """Grid Bayes filter over 0.25 m cells: between ticks you can move ~sigma metres on the same
    floor (Gaussian kernel); a small epsilon lets strong evidence move you anywhere, including
    another floor. Evidence is tempered because overlapping windows share samples."""

    # (motion_db upper bound, kernel sigma m, escape eps): still / shifting / walking.
    # Process noise is a VELOCITY x dt. 0.25 m/tick for "still" was a slow walk (0.125 m/s) and
    # compounded to ~2.5 m of blur over 100 ticks, widening the posterior instead of sharpening it
    # (measured 2026-09-23: r68 3.8 -> 5.2 m after 3.5 min still). 0.1 m on a 0.25 m grid is the
    # identity: no diffusion, evidence accumulates, and the small escape keeps it movable.
    # + lambda: fading-memory power on the carried belief. Steady-state evidence ~ lik^(beta/(1-lam)):
    # still 0.97 with beta 0.067 -> lik^2.2 (about two full windows, not unbounded), memory ~74 s.
    # Unbounded accumulation sharpened onto whatever the first noisy window said and would not let
    # go (Nick 2026-09-23: "takes forever to move and converge, median error increased").
    LEVELS = ((2.0, 0.1, 0.002, 0.97), (5.0, 0.5, 0.01, 0.92), (1e9, 1.4, 0.02, 0.85))

    def __init__(self, cxy, cfi, sigma=None, eps=None):
        from scipy import sparse
        n = len(cxy)
        self.K = {}
        for _, sg, _, _ in self.LEVELS:
            rows, cols, vals = [], [], []
            for fi in np.unique(cfi):
                idx = np.nonzero(cfi == fi)[0]
                xy = cxy[idx]
                for a, i in enumerate(idx):
                    d2 = np.sum((xy - xy[a]) ** 2, axis=1)
                    nb = np.nonzero(d2 <= (2 * sg) ** 2)[0]
                    rows += [i] * len(nb)
                    cols += list(idx[nb])
                    vals += list(np.exp(-d2[nb] / (2 * sg * sg)))
            K = sparse.csr_matrix((vals, (rows, cols)), shape=(n, n))
            self.K[sg] = sparse.csr_matrix(K.multiply(1.0 / np.asarray(K.sum(axis=0)).ravel()[None, :]))
        self.n = n
        self.post = None
        self.level = None
        self.ticks = 0                      # ticks since reset; gates the verdict during warm-up

    def pick(self, motion_db):
        m = 1e9 if motion_db is None else float(motion_db)
        for ub, sg, eps, lam in self.LEVELS:
            if m < ub:
                return sg, eps, lam
        return self.LEVELS[-1][1:]

    def reset(self):
        self.post = None
        self.ticks = 0

    def fresh(self):
        """A new filter (own belief) sharing this one's kernels - they depend only on the grid."""
        f = object.__new__(MotionFilter)
        f.K, f.n, f.level = self.K, self.n, None
        f.reset()
        return f

    def step(self, lik, beta, motion_db=None):
        sg, eps, lam = self.pick(motion_db)
        self.level = sg
        self.ticks += 1
        if self.post is None:
            # First window after a reset is all-new evidence but also the noisiest one; a
            # full-strength first tick locked the filter onto it (measured 2026-09-23: p 0.90
            # CALL on the wrong room at t+30 s). Half strength: fast start, no lock-in.
            prior, beta = np.full(self.n, 1.0 / self.n), 0.5
        else:
            prior = self.K[sg] @ self.post
            prior = np.power(np.maximum(prior, 1e-300), lam)      # fading memory
            prior = prior / prior.sum()
        prior = (1.0 - eps) * prior + eps / self.n
        post = prior * np.power(np.maximum(lik, 1e-300), beta)
        s = post.sum()
        self.post = post / s if s > 0 and np.isfinite(s) else np.full(self.n, 1.0 / self.n)
        return self.post


class Engine:
    def __init__(self, log=print):
        t0 = time.time()
        self.D = B.Data()
        self.house = self.D.house
        self.scanners = list(self.D.scanners)
        # A house's own fit needs enough calibration spots (locations): on a household's first one to three a fit is
        # worse than the default model (EXPERIMENTS 2026-10-01, bakeoff.py spots), so below RP_MIN_SPOTS it runs on that.
        need = int(os.environ.get("RP_MIN_SPOTS", "5"))
        trainable = [e for e in self.D.E if CL.trainable(e)]
        spots = {e["group"] for e in trainable}
        groups = sorted({e["group"] for e in self.D.E}) if len(spots) >= need else []
        self.fits = self.D.fit_all(groups)
        self.trained = dict(spots=len(spots), rounds=len({e["round"] for e in trainable if e.get("round")}),
                            default=not groups, needed=need)
        self.models = [m for m in MODELS if m in self.fits]
        P, meta = self.D.P, self.D.meta
        self.P = P
        self.nP = len(P)
        # candidate points -> cells (the posterior is reported per 0.25 m cell, heights summed)
        uniq, first = {}, []
        gidx = np.empty(self.nP, np.int64)
        for i, m in enumerate(meta):
            k = (m[0], m[3], m[4])
            if k not in uniq:
                uniq[k] = len(uniq)
                first.append(i)
            gidx[i] = uniq[k]
        self.gidx = gidx
        self.nC = len(uniq)
        first = np.array(first)
        self.cell_xy = P[first, :2]
        self.cell_fi = np.array([meta[i][0] for i in first])
        self.cell_room = [meta[i][1] for i in first]
        # predicted RSSI, once
        self.MU = {m: np.stack([M.mu_s(m, self.fits[m], s, self.D.cbank[s])
                                for s in self.scanners]).astype(np.float64) for m in self.models}
        self.sigma = {m: max(4.0, 1.3 * self.fits[m].mad) for m in self.models}
        self.sigma = {m: v * M.SIGMA_SCALE for m, v in self.sigma.items()}   # see models.SIGMA_SCALE
        self.bias = {m: self.fits[m].bias() for m in self.models}
        # rooms + filter
        self.rooms = sorted(set(zip(self.cell_fi.tolist(), self.cell_room)), key=lambda r: (r[0], r[1]))
        ridx = {r: i for i, r in enumerate(self.rooms)}
        self.cell_ridx = np.array([ridx[(f, r)] for f, r in zip(self.cell_fi.tolist(), self.cell_room)])
        self.room_ncells = np.bincount(self.cell_ridx, minlength=len(self.rooms)).astype(float)
        self.graph = room_graph(self.house)
        outdoor = {r for r, k in self.house.kinds.items() if k == "outdoor"}
        closets = {r for r, k in self.house.kinds.items() if k == "closet"}
        self.room_w = room_weights(self.rooms, outdoor, closets)
        self.floor_names = list(self.house.names_floor)
        self.cell_prior = cell_prior(self.cell_ridx, self.room_w)       # the same prior on the one-window cell posterior
        self.filters = {m: RoomFilter(self.rooms, self.graph, weights=self.room_w) for m in self.models}
        self.motion = {m: MotionFilter(self.cell_xy, self.cell_fi) for m in self.models}
        self.dev_state = {}                 # device key -> (room filters, motion filters), see _state
        self.hashes = code_hashes(self.house)
        log(f"engine ready in {time.time() - t0:.1f}s: {len(self.scanners)} scanners, {self.nP} points, "
            f"{self.nC} cells, {len(self.rooms)} rooms, models {self.models}, " +
            (f"on the default model ({len(spots)} of the {need} calibration spots a fit needs)" if not groups
             else f"fitted on {len(spots)} calibration spots"))

    # ------------------------------------------------------------------------------------
    def anchor_correction(self, stats, min_anchors=2, min_now=5, max_spread=8.0, sigma=1.5, sigma_esp=3.0):
        """Differential correction per scanner from its fixed-anchor links (see patch_anchorcorr).
        Model-independent: returns {scanner: dB} to add to that scanner's expected level.

        The reference is the fit-epoch snapshot (collector.anchor_stats), so a delta is "how far this
        receiver reads from where the fit knew it". Echo Shows really do wander (7 dB overnight) and
        get the full correction (shrink sigma 1.5). ESP32 receivers hold within ~1 dB, so a delta on
        one of their anchor links is more likely the environment (car in the garage, a door) than
        the radio: shrink harder (sigma_esp) so only a large, consistent delta gets through."""
        corr, detail = {}, {}
        for s in self.scanners:
            deltas, spans = [], []
            for key, per in stats.items():
                v = per.get(s)
                if v and v["now_n"] >= min_now:
                    deltas.append(v["now"] - v["ref"]); spans.append(v["span_s"])
            if len(deltas) >= min_anchors and (max(deltas) - min(deltas)) <= max_spread:
                c = float(np.median(deltas))
                sg = sigma if M.is_echo(s) else sigma_esp
                c = c * (c * c) / (c * c + sg * sg)                  # shrink noise-level values to ~0
                corr[s] = round(c, 1)
                detail[s] = dict(raw=round(float(np.median(deltas)), 1), anchors=len(deltas),
                                 span_s=int(np.median(spans)))
            else:
                corr[s] = 0.0
                detail[s] = dict(raw=None, anchors=len(deltas), span_s=None)
        self.anchor_detail = detail
        return corr

    def _mu(self, model, dtype):
        """The predicted-RSSI table in the posterior's precision (the float32 copy is made once per model)."""
        if dtype == np.float64:
            return self.MU[model]
        cache = self.__dict__.setdefault("_mu32", {})
        if model not in cache:                       # a race here only makes the same copy twice
            cache[model] = np.ascontiguousarray(self.MU[model], dtype=np.float32)
        return cache[model]

    def posterior(self, model, obs, dead=(), min_n=LIVE_MIN_N, off_sd=4.0, window_noise=True, k_adj=None, off_mean=0.0,
                  exact=None):
        """Cell posterior (nC,) and per-room likelihood. Same math as models.localise, plus:

        window_noise: v3's sigma was calibrated on ~180 s medians. A short live window's median
        is noisier, and ignoring that made the live view MORE confident on less data (86% vs
        v3's 39% on the same capture). Each scanner's sigma is widened by the median's own
        standard error, ~1.25 * 5 dB / sqrt(n). Negligible for long captures; off for parity.

        off_mean: the centre of the device's transmit-offset prior (running calibration, spec 2026-09-27; 0 = the
        phone scale). The returned `offset` is this window's own estimate of it (posterior mean over every point).

        exact: the float64 / 0.5 dB / scipy math (True) or the cheap one (False); None = RP_EXACT_POSTERIOR.
        """
        dead = set(dead or ())
        det, cen = M.split_obs(obs, min_n)
        det = {s: v for s, v in det.items() if s not in dead and s in self.scanners}
        cen = [s for s in cen if s not in dead and s in self.scanners] + \
              [s for s in self.scanners if s not in obs and s not in dead]
        if not det:
            return None
        exact = EXACT_POSTERIOR if exact is None else bool(exact)
        nu, sig = M.NU, self.sigma[model]
        if exact:
            ft, step, span, lcdf = np.float64, 0.5, max(15.0, 2.5 * off_sd), log_ndtr
        else:
            ft, step, span, lcdf = np.float32, FAST_OFF_STEP, min(FAST_OFF_SPAN, max(15.0, 2.5 * off_sd)), log_ndtr_fast
        O = (float(off_mean) + np.arange(-span, span + 0.01, step)).astype(ft)
        tot = np.zeros((self.nP, len(O)), ft)
        MU = self._mu(model, ft)
        for i, s in enumerate(self.scanners):
            adj = float((k_adj or {}).get(s, 0.0))          # live receiver bias (Echo Shows)
            ex2 = M.extra_sd(s) ** 2                         # Shows are trusted less
            if s in det:
                sg2 = sig * sig + ex2 + ((6.25 / math.sqrt(max(obs[s]["n"], 1))) ** 2 if window_noise else 0.0)
                r = (ft(det[s] - adj) - MU[i])[:, None] - O[None, :]
                r *= r
                r *= ft(1.0 / (nu * sg2))
                tot += ft(-(nu + 1) / 2) * np.log1p(r)
            elif s in cen:
                z = (ft(M.CENSOR_AT - adj) - MU[i])[:, None] - O[None, :]
                z *= ft(1.0 / math.sqrt(sig * sig + ex2))
                tot += lcdf(z)
        tot += (-0.5 * ((O.astype(np.float64) - float(off_mean)) / off_sd) ** 2).astype(ft)[None, :]
        mx = tot.max(axis=1, keepdims=True)
        mo = tot.max(axis=0)
        wo = np.exp((mo - mo.max()).astype(np.float64)) * np.exp(tot - mo[None, :]).sum(axis=0, dtype=np.float64)
        offset = float((wo * O).sum() / wo.sum())                                # offset marginal over every point
        lp = mx[:, 0].astype(np.float64) + np.log(np.exp(tot - mx).sum(axis=1, dtype=np.float64))
        top = lp.max()
        w = np.exp(lp - top)
        cellw = np.bincount(self.gidx, weights=w, minlength=self.nC)
        post = apply_prior(cellw / cellw.sum(), getattr(self, "cell_prior", 1.0))
        roomw = np.bincount(self.cell_ridx, weights=cellw, minlength=len(self.rooms))
        room_like = roomw / np.maximum(self.room_ncells, 1)
        room_like = room_like / room_like.max()
        return dict(post=post, room_like=room_like, det=sorted(det), censored=sorted(cen), offset=offset)

    def summarise(self, post):
        R = dict(post=post, xy=self.cell_xy, fi=self.cell_fi, room=self.cell_room)
        S = M.summarise(R, self.house)
        fl = S["room_floor"]
        z = float(self.house.Z[fl] + 1.0)
        return dict(floor=int(fl), floor_name=self.floor_names[fl], p_floor=float(S["p_floor"]), room=S["room"],
                    p_room=float(S["p_room"]), verdict=S["verdict"], xy=[float(S["xy"][0]), float(S["xy"][1])],
                    map_floor=int(S["map_floor"]), z=z, r68=float(S["r68"]),
                    top=[dict(floor=int(f), room=r, p=float(p)) for (f, r), p in S["top"]],
                    wall_clear=float(S["wall_clear"]))

    def heat(self, post, max_cells=900, rel=1e-3):
        keep = np.nonzero(post >= post.max() * rel)[0]
        if len(keep) > max_cells:
            keep = keep[np.argsort(-post[keep])[:max_cells]]
        return [[int(self.cell_fi[i]), round(float(self.cell_xy[i, 0]), 3), round(float(self.cell_xy[i, 1]), 3),
                 round(float(post[i]), 5)] for i in keep]

    def bermuda_sim(self, obs, dead=(), bias=None, min_n=LIVE_MIN_N):
        det, _ = M.split_obs(obs, min_n)
        det = {s: v for s, v in det.items() if s not in set(dead) and s in self.scanners}
        if not det:
            return None
        score = {s: v - (bias or {}).get(s, 0.0) for s, v in det.items()}
        s = max(score, key=score.get)
        v = self.D.S[s]
        return dict(scanner=s, room=v["room"], floor=self.house.floor_of_z(v["z_abs"]),
                    floor_name=self.floor_names[self.house.floor_of_z(v["z_abs"])])

    def _state(self, device):
        """(room filters, motion filters) by model for one tracked device; device None = the shared
        default set (parity checks, single-device callers)."""
        if device is None:
            return self.filters, self.motion
        st = self.dev_state.get(device)
        if st is None:
            st = ({m: f.fresh() for m, f in self.filters.items()}, {m: f.fresh() for m, f in self.motion.items()})
            self.dev_state[device] = st
        return st

    def drop_devices(self, keep):
        """Forget the filter state of devices no longer tracked."""
        for k in list(self.dev_state):
            if k not in keep:
                del self.dev_state[k]

    def step(self, obs, dead, dt, halflife, kind="phone", use_filter=True, motion=False, k_adj=None,
             motion_db=None, device=None, models=None, off_mean=None):
        """One live update for one device. obs: {scanner: {med, n}}. models: the subset to run
        (the extra tracked devices run only the default model, to bound CPU)."""
        off_sd = 4.0 if kind == "phone" else 12.0
        filters, motionf = self._state(device)
        out = {}
        ns = sorted(o["n"] for o in obs.values() if o["n"] >= LIVE_MIN_N)
        warming = (not ns) or ns[len(ns) // 2] < 5      # too few samples to back a confident call
        beta = float(min(1.0, dt / max(2.0 * halflife, dt)))   # overlapping windows share samples
        for m in (models or self.models):
            kw = {} if off_mean is None else dict(off_mean=off_mean)       # the device's running offset
            R = self.posterior(m, obs, dead, off_sd=off_sd, k_adj=(k_adj or {}).get(m), **kw)
            if R is None:
                out[m] = None
                continue
            post = motionf[m].step(R["post"], beta, motion_db) if motion else R["post"]
            S = self.summarise(post)
            S["heat"] = self.heat(post)
            S["motion"] = bool(motion)
            S["diffusion_m"] = motionf[m].level if motion else None
            S["used"], S["censored"] = R["det"], R["censored"]
            S["offset"] = R.get("offset")
            if warming or (motion and motionf[m].ticks < WARMUP_TICKS):
                S["verdict"], S["warming"] = "LOW-CONF", True
            if use_filter:
                b = filters[m].step(R["room_like"], dt, beta, tau=filter_tau(kind))
                order = np.argsort(-b)[:3]
                S["filtered"] = [dict(floor=int(self.rooms[i][0]), room=self.rooms[i][1], p=float(b[i])) for i in order]
                # The filter is the authority (Nick 2026-09-26, the state machine): the reported room and floor
                # are its top belief - a device cannot jump to a room the graph does not connect on one window's
                # evidence - and the position is the best cell of this window INSIDE that room. The raw call is
                # kept for diagnostics.
                top = int(order[0])
                S["raw"] = dict(room=S.get("room"), floor=S.get("floor"), xy=list(S.get("xy") or []), p_room=S.get("p_room"))
                fl, rm = int(self.rooms[top][0]), self.rooms[top][1]
                S["room"], S["floor"], S["floor_name"], S["map_floor"], S["p_room"], S["gated"] = rm, fl, self.floor_names[fl], fl, float(b[top]), True
                sel = self.cell_ridx == top
                if sel.any():
                    j = int(np.flatnonzero(sel)[np.argmax(post[sel])])
                    S["room_xy"] = S["xy"] = [float(self.cell_xy[j, 0]), float(self.cell_xy[j, 1])]
                    S["room_xy_floor"] = int(self.cell_fi[j])
                    S["room_xy_room"] = rm
                    S["z"] = float(self.house.Z[fl] + 1.0) if hasattr(self, "house") else S.get("z")
            out[m] = S
        out["Bermuda (sim)"] = self.bermuda_sim(obs, dead)
        out["Bermuda+offsets (sim)"] = self.bermuda_sim(obs, dead, self.bias.get("FreeSpace"))
        return out

    def known_offsets(self, model="DominantPath"):
        """{device key or tag id: dB above the phone scale} known before any live learning - where running device
        offsets start (spec 2026-09-27): the fit's per-device offsets for the fixed anchors, and the survey tags'
        levels from the co-location rounds (keyed by tag id; the server maps them to device keys)."""
        F = self.fits.get(model) or next(iter(self.fits.values()))
        out = {k[4:]: float(v) for k, v in F.o.items() if k.startswith("dev:")}
        d = B.fresh_dir()
        if d:
            import calib_loader as CL
            import fresh_data as FD
            out.update({t: float(v) for t, v in FD.tag_offsets(CL.calib_events(self.house, os.path.join(d, "calib")),
                                                               FD.load_levels(d)).items()})
        return out

    def reset_filters(self, device=None):
        """Reset one device's filters, or (device None) every device's."""
        sets = [self._state(device)] if device is not None else [(self.filters, self.motion)] + list(self.dev_state.values())
        for filters, motionf in sets:
            for f in list(filters.values()) + list(motionf.values()):
                f.reset()

    # ------------------------------------------------------------------------------------
    def parity(self, capture):
        """Prove the live engine reproduces solver/models.localise on a recorded capture."""
        obs, _ = B.rawcap_obs(capture)
        dead, _ = B.dead_scanners(capture)
        rep = {}
        for m in self.models:
            ref = M.localise(m, self.fits[m], obs, self.scanners, self.D.cbank, self.D.meta, self.D.P,
                             min_n=10, off_sd=4.0, dead=dead)
            mine = self.posterior(m, obs, dead, min_n=10, off_sd=4.0, window_noise=False, exact=True)
            a, b = ref["post"], mine["post"]
            rep[m] = dict(max_abs_diff=float(np.max(np.abs(a - b))), same_map=int(np.argmax(a)) == int(np.argmax(b)),
                          ref_room=M.summarise(ref, self.house)["room"], mine_room=self.summarise(b)["room"])
        return rep
