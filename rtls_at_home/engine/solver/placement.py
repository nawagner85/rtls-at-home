"""The placement advisor (spec docs/specs/2026-10-01-placement-advisor-design.md): where should the proxies go?

Generic `place_opt`. Candidates are the surveyed outlets (not the deaf ones) at antenna height plus where each movable
proxy stands now; the receivers that stay put (the Echo Shows by default) are fixed. A layout is scored the way
place_opt does: the live DominantPath fit as truth, every candidate at a healthy proxy's level (the median of today's
proxies), devices at pocket height in every indoor room with noise, located the engine's way (an unknown transmitter
integrated out, censoring), and scored by the room and floor the posterior mass picks. Search: greedy, then single
swaps until none helps; the greedy curve runs a little past the asked number ("one more proxy adds ...").

    python placement.py --job job.json --out result.json      (JSON progress lines on stdout)
"""
import json
import math
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import numpy as np  # noqa: E402

IN = 0.0254
ANTENNA_M = 3.5 * IN            # a XIAO's antenna starts ~3.5 in above its outlet (outlet_survey.ANTENNA_OFFSET_M)
MERGE_M = 0.3                   # candidates closer than this on one floor are one spot
DEFAULT_K = -60.0               # a proxy's level when the house has no proxy yet to take the median of
DEFAULTS = dict(per_room=20, draws=3, seed=7, sd_proxy=4.0, sd_show=6.0, off_sd=12.0, extra=2)


class Cancelled(Exception):
    """The search was asked to stop."""


def J(sc):
    return sc[0] + sc[1]


def search(cands, n, score, fixed=(), extra=2, progress=None, cancelled=lambda: False):
    """Greedy to n + extra (the curve), then single swaps from the greedy n until none helps.
    score(keys) -> (room accuracy, floor accuracy). Returns {chosen, score, curve: [(k, score)]}."""
    fixed, chosen, curve, at_n = list(fixed), [], [], None
    steps = min(len(cands), n + extra)
    for k in range(steps):
        if cancelled():
            raise Cancelled()
        best = max((c for c in cands if c not in chosen), key=lambda c: J(score(fixed + chosen + [c])))
        chosen.append(best)
        curve.append((k + 1, score(fixed + chosen)))
        if progress:
            progress(dict(step="greedy", k=k + 1, of=steps))
        if k + 1 == n:
            at_n = list(chosen)
    cur_chosen = at_n if at_n is not None else list(chosen)
    cur = score(fixed + cur_chosen)
    improved, rnd = True, 0
    while improved:
        improved, rnd = False, rnd + 1
        for i in range(len(cur_chosen)):
            for c in cands:
                if cancelled():
                    raise Cancelled()
                if c in cur_chosen:
                    continue
                trial = cur_chosen[:i] + [c] + cur_chosen[i + 1:]
                sc = score(fixed + trial)
                if J(sc) > J(cur) + 1e-9:
                    cur_chosen, cur, improved = trial, sc, True
            if progress:
                progress(dict(step="swaps", round=rnd, at=i + 1, of=len(cur_chosen), score=list(cur)))
    return dict(chosen=cur_chosen, score=cur, curve=curve)


def assign(chosen, movable, pos):
    """Which proxy goes where: a proxy whose own spot ("now:<name>") is chosen stays; the other chosen spots go to the
    remaining proxies by the least total distance (3-D); a spot left over is a new proxy (receiver None) and a proxy
    left over is not needed. pos: {key: (x, y, z)} with "now:<name>" for every movable proxy."""
    from scipy.optimize import linear_sum_assignment
    moves, free_c, free_r = [], [], list(movable)
    for c in chosen:
        if c.startswith("now:") and c[4:] in free_r:
            moves.append(dict(candidate=c, receiver=c[4:]))
            free_r.remove(c[4:])
        else:
            free_c.append(c)
    paired = {}
    if free_c and free_r:
        cost = np.array([[math.dist(pos[c], pos["now:" + r]) for r in free_r] for c in free_c])
        ci, ri = linear_sum_assignment(cost)
        paired = {free_c[i]: free_r[j] for i, j in zip(ci, ri)}
    moves += [dict(candidate=c, receiver=paired.get(c)) for c in free_c]
    return dict(moves=moves, unneeded=[r for r in free_r if r not in paired.values()])


def candidates(outlets, elev, movable_now, antenna=ANTENNA_M, merge=MERGE_M):
    """{key: {x, y, z_abs, room, floor, outlet}}: each movable proxy's spot ("now:<name>") and every usable outlet
    ("out:<id>") at antenna height - not deaf, placed, on a floor of the house, not within `merge` of a spot already
    listed on its floor. elev: floor elevations, bottom first; outlets' floor is an index. A proxy's spot takes the
    id of the nearest outlet merged into it (the outlet it is plugged into), so the map can show it."""
    out = {f"now:{n}": dict(x=v["x"], y=v["y"], z_abs=v["z_abs"], room=v.get("room"), floor=v["floor"], outlet=None)
           for n, v in movable_now.items()}
    near = {}                                           # now: key -> distance of the outlet it took
    for o in outlets:
        fi = o.get("floor")
        if o.get("x") is None or o.get("y") is None or "deaf" in str(o.get("status") or "").lower():
            continue
        if not isinstance(fi, int) or not 0 <= fi < len(elev):
            continue
        d, k = min(((math.hypot(c["x"] - o["x"], c["y"] - o["y"]), k) for k, c in out.items() if c["floor"] == fi),
                   default=(math.inf, None))
        if d < merge:
            if k.startswith("now:") and d < near.get(k, math.inf):
                out[k]["outlet"], near[k] = o["id"], d
            continue
        out[f"out:{o['id']}"] = dict(x=o["x"], y=o["y"], z_abs=elev[fi] + float(o.get("z_rel") or 0) + antenna,
                                     room=o.get("room"), floor=fi, outlet=o["id"])
    return out


_ZT0, _ZT1, _ZST = -40.0, 10.0, 0.01                 # engine.log_ndtr_fast's table, in float64


def _ztab():
    from scipy.special import log_ndtr
    t = log_ndtr(np.arange(_ZT0, _ZT1 + _ZST / 2, _ZST))
    return t, np.append(np.diff(t), 0.0)


def log_ndtr_fast(z, tab):
    """log Phi(z) by linear interpolation in a 0.01-step table over [-40, 10] (error < 1e-4 nats), clipped outside."""
    t, slope = tab
    u = (np.clip(z, _ZT0, _ZT1) - _ZT0) * (1.0 / _ZST)
    i = np.minimum(u.astype(np.int32), len(t) - 1)
    return t[i] + (u - i) * slope[i]


class Scorer:
    """place_opt's layout score: truth points tj (indices into the grid) with noisy readings from every key's
    prediction MU[k], located over the whole grid with an unknown transmitter offset integrated out (prior sd off_sd)
    and the unheard keys censored at censor_at; (room accuracy, floor accuracy) of the posterior's argmax room and
    floor. The same maths as place_opt's loop, arranged to be fast: the heard keys' sums as matrix products, a
    censored term only where a key is unheard (by the engine's log Phi table), room and floor mass by one-hot
    products."""

    def __init__(self, MU, sd_model, sd_noise, noise, tj, p_room, p_floor, n_rooms, n_floors, off_sd, censor_at,
                 chunk=64):
        self.MU, self.sd_model, self.sd_noise, self.noise = MU, sd_model, sd_noise, noise
        self.tj, self.p_room, self.p_floor = np.asarray(tj), np.asarray(p_room), np.asarray(p_floor)
        self.n_rooms, self.n_floors, self.off_sd, self.censor_at = n_rooms, n_floors, off_sd, censor_at
        self.chunk, self.draws = chunk, len(next(iter(noise.values())))
        self.t_room, self.t_floor = self.p_room[self.tj], self.p_floor[self.tj]
        n = len(self.p_room)
        self.R = np.zeros((n, n_rooms))
        has = self.p_room >= 0
        self.R[np.nonzero(has)[0], self.p_room[has]] = 1.0
        self.F = np.zeros((n, n_floors))
        self.F[np.arange(n), self.p_floor] = 1.0
        self.tab = _ztab()

    def score(self, keys):
        Mk = np.stack([self.MU[k] for k in keys])                        # K x P
        M2 = Mk * Mk
        w = np.array([1.0 / self.sd_model[k] ** 2 for k in keys])
        sd = np.array([self.sd_model[k] for k in keys])
        T, rr, fr = len(self.tj), 0, 0
        for d in range(self.draws):
            obs = np.stack([Mk[j][self.tj] + self.sd_noise[k] * self.noise[k][d] for j, k in enumerate(keys)], 1)
            heard = obs > self.censor_at                                 # T x K
            hw = heard * w
            for a in range(0, T, self.chunk):
                b = min(T, a + self.chunk)
                HW, HWO = hw[a:b], hw[a:b] * obs[a:b]
                swd = HWO.sum(1, keepdims=True) - HW @ Mk                 # sum_k h w (obs - mu)
                dd = (HWO * obs[a:b]).sum(1, keepdims=True) - 2 * HWO @ Mk + HW @ M2
                o = swd / (HW.sum(1, keepdims=True) + 1.0 / self.off_sd ** 2)
                ll = -0.5 * (dd - swd * o)
                cen = ~heard[a:b]
                for j in range(len(keys)):
                    rows = np.nonzero(cen[:, j])[0]
                    if rows.size:
                        ll[rows] += log_ndtr_fast((self.censor_at - Mk[j][None, :] - o[rows]) / sd[j], self.tab)
                post = np.exp(ll - ll.max(axis=1, keepdims=True))
                rr += int(((post @ self.R).argmax(axis=1) == self.t_room[a:b]).sum())
                fr += int(((post @ self.F).argmax(axis=1) == self.t_floor[a:b]).sum())
        return rr / (self.draws * T), fr / (self.draws * T)


def prepare(job, progress=lambda p: None):
    """Everything a search or a check scores with, for job {fixed: [receiver names], outlets, params}: the live fit,
    the spots (candidates), the fixed receivers' keys and the scorer. A namespace."""
    from collections import defaultdict
    from types import SimpleNamespace
    import bakeoff as B
    import geom3d as g
    import models as M
    prm = {**DEFAULTS, **(job.get("params") or {})}
    progress(dict(step="prepare"))
    D = B.Data()
    F = D.fit_all(sorted({e["group"] for e in D.E}), ["DominantPath"])["DominantPath"]
    house, P, meta = D.house, D.P, D.meta
    elev = [float(z) for z in house.Z]
    fixed = [s for s in job.get("fixed") or [] if s in D.scanners]
    movable = [s for s in D.scanners if s not in fixed]
    now = {s: dict(x=D.S_now[s]["x"], y=D.S_now[s]["y"], z_abs=D.S_now[s]["z_abs"], room=D.S_now[s].get("room"),
                   floor=house.floor_of_z(D.S_now[s]["z_abs"])) for s in movable}
    C = candidates(job.get("outlets") or [], elev, now)
    proxies = [s for s in D.scanners if not M.is_echo(s) and s in F.K]
    K = float(np.median([F.K[s] for s in proxies])) if proxies else DEFAULT_K
    sig_fit = max(4.0, 1.3 * F.mad) * M.SIGMA_SCALE
    S = {k: dict(x=v["x"], y=v["y"], z_abs=v["z_abs"], room=v["room"]) for k, v in C.items()}
    unfit = [s for s in fixed if s not in F.K]
    for s in unfit:
        S["fix:" + s] = dict(x=D.S_now[s]["x"], y=D.S_now[s]["y"], z_abs=D.S_now[s]["z_abs"], room=D.S_now[s].get("room"))
    echos = [F.K[s] for s in D.scanners if M.is_echo(s) and s in F.K]
    K_echo = float(np.median(echos)) if echos else K
    progress(dict(step="geometry", candidates=len(S)))
    _, bank = g.cached_bank(house, S, P, "place")
    keep = np.array([house.room_kind(m[0], m[1]) == "outdoor" for m in meta], dtype=float)
    for k in bank:
        bank[k]["k5"] = bank[k]["k5"] * keep
        bank[k]["echo"] = np.zeros(len(P))
    MU = {k: np.asarray(M.mu("DominantPath", F.p, K, bank[k]), float) for k in S if not k.startswith("fix:")}
    for s in fixed:
        if s in F.K:
            MU["fix:" + s] = np.asarray(M.mu_s("DominantPath", F, s, D.cbank[s]), float)
        else:                                           # no fit level yet (not captured since it was placed): a
            b = dict(bank["fix:" + s], echo=np.full(len(P), 1.0 if M.is_echo(s) else 0.0))   # typical one of its kind
            MU["fix:" + s] = np.asarray(M.mu("DominantPath", F.p, K_echo if M.is_echo(s) else K, b), float)
    sd_model = {k: math.sqrt(sig_fit ** 2 + M.extra_sd(k[4:] if k.startswith("fix:") else "x") ** 2) for k in MU}
    sd_noise = {k: prm["sd_show"] if (k.startswith("fix:") and M.is_echo(k[4:])) else prm["sd_proxy"] for k in MU}

    rng = np.random.default_rng(prm["seed"])
    hh = min(sorted({m[2] for m in meta}), key=lambda h: abs(h - 1.0))
    byroom = defaultdict(list)
    for i, m in enumerate(meta):
        if m[2] == hh and m[1] and house.room_kind(m[0], m[1]) != "outdoor":
            byroom[(m[0], m[1])].append(i)
    truth = [(key, j) for key, idx in sorted(byroom.items())
             for j in rng.choice(idx, size=min(prm["per_room"], len(idx)), replace=False)]
    rooms = sorted({(m[0], m[1]) for m in meta if m[1]})
    ridx = {r: i for i, r in enumerate(rooms)}
    tj = np.array([j for _, j in truth])
    noise = {k: rng.standard_normal((prm["draws"], len(tj))) for k in MU}
    scorer = Scorer(MU, sd_model, sd_noise, noise, tj, p_room=[ridx.get((m[0], m[1]), -1) for m in meta],
                    p_floor=[m[0] for m in meta], n_rooms=len(rooms), n_floors=house.NF, off_sd=prm["off_sd"],
                    censor_at=M.CENSOR_AT)
    progress(dict(step="truth", points=len(tj), rooms=len(byroom)))
    return SimpleNamespace(house=house, elev=elev, fixed=fixed, unfit=unfit, movable=movable, C=C, K=K, prm=prm,
                           score=scorer.score, T=len(tj), fixed_keys=["fix:" + s for s in fixed])


def check(job, progress=lambda p: None):
    """A selection's score (spec section 4): job["check"] lists the spots (keys of a suggestion: "now:<proxy>" for
    a proxy left where it is, "out:<outlet>" for a ticked target), scored with the fixed receivers as a search scores
    a layout. ValueError naming the spots that are no longer candidates."""
    keys = list(job.get("check") or [])
    x = prepare(job, progress)
    gone = [k for k in keys if k not in x.C]
    if gone:
        raise ValueError(f"{', '.join(gone)}: not a spot any more (the house, the receivers or the outlets changed) - "
                         "run the advisor again")
    room, floor = x.score(x.fixed_keys + keys)
    return dict(keys=keys, room=round(room, 4), floor=round(floor, 4))


def run(job, progress=lambda p: None, cancelled=lambda: False):
    """The advice for job {proxies, fixed: [receiver names], outlets, params}: today's layout and the suggestion
    (room and floor accuracy), the curve, the moves (candidate, receiver, outlet, floor, x, y, height above the
    floor) and the proxies not needed."""
    x = prepare(job, progress)
    house, elev, fixed, unfit, movable, C, K, prm, score, T, fixed_keys = (
        x.house, x.elev, x.fixed, x.unfit, x.movable, x.C, x.K, x.prm, x.score, x.T, x.fixed_keys)
    today = score(fixed_keys + ["now:" + s for s in movable])
    progress(dict(step="today", score=list(today)))
    r = search(list(C), int(job["proxies"]), score, fixed=fixed_keys, extra=prm["extra"], progress=progress,
               cancelled=cancelled)
    a = assign(r["chosen"], movable, {k: (v["x"], v["y"], v["z_abs"]) for k, v in C.items()})
    sc = lambda s: dict(room=round(s[0], 4), floor=round(s[1], 4))
    moves = []
    for m in a["moves"]:
        c = C[m["candidate"]]
        moves.append(dict(m, outlet=c["outlet"], floor=c["floor"], floor_id=house.ids[c["floor"]], x=c["x"], y=c["y"],
                          room=c["room"], height=round(c["z_abs"] - elev[c["floor"]], 4)))
    return dict(today=sc(today), suggested=sc(r["score"]), curve=[dict(n=k, **sc(s)) for k, s in r["curve"]],
                moves=moves, unneeded=a["unneeded"], fixed=fixed, unfit=unfit, movable=movable, level=round(K, 1),
                params=prm, points=T, candidates=len(C))


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    with open(args.job, encoding="utf-8") as f:
        job = json.load(f)

    def progress(p):
        print(json.dumps(p), flush=True)

    try:
        result = check(job, progress) if "check" in job else run(job, progress)
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), flush=True)
        sys.exit(1)
    d = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(result, f)
    os.replace(tmp, args.out)
    print(json.dumps({"done": True}), flush=True)


if __name__ == "__main__":
    main()
