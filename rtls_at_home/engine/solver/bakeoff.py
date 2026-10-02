"""Model bake-off: train on an installation's survey and calibration data, score by leave-one-location-out,
then run blind tests with predictions written to disk BEFORE the truth is revealed.

  python bakeoff.py cv                         # fit everything, leave-one-location-out
  python bakeoff.py defaults                   # every event located on the default model alone (no fitting)
  python bakeoff.py locate <capture.json>      # blind prediction, all models, saved to disk
  python bakeoff.py score <pred.json> X Y FLOOR   # after the reveal

Training data (events()): a fresh survey folder (RP_FRESH, solver/fresh_data.py) where the installation has one,
else the developer's older survey where present (solver/legacy_survey.py), plus the Calibrate tab's rounds
(solver/calib_loader.py); a new household has only its rounds, and below RP_MIN_SPOTS the engine runs on the default
model (solver/model_defaults.json).
"""
import bisect, glob, json, math, os, sys, time, statistics as st
from datetime import datetime
import numpy as np
import geom3d as g
import models as M
import calib_loader as CL

HERE = os.path.dirname(os.path.abspath(__file__))
V2 = os.environ.get("RP_V2", "1") == "1"      # v2 (default): +blind points in training
WAPS = os.environ.get("RP_WAPS", "1") == "1"  # v3 (default): the three Omada WAP iBeacons as anchors
WAPUP = os.environ.get("RP_WAPUP", "0") == "1"  # + back-lobe term for scanners above a ceiling AP
SURV = os.path.join(HERE, "..", "survey")
IN = 0.0254
class _FloorNames:
    """FLOORNAME[fi]: the house's floor names (house.json), read once; reset() reads them again."""

    def __init__(self):
        self._names = None

    def reset(self):
        self._names = None

    def __getitem__(self, fi):
        if self._names is None:
            import house_doc as HD
            self._names = [f["name"] for f in HD.load()["floors"]]
        return self._names[fi]


FLOORNAME = _FloorNames()


_BUILD = []


def fixture_centre(fid, name):
    """Centre of a built fixture (after the build pushed it off any wall), from the house's build folder."""
    if not _BUILD:
        import house_build as HB
        import house_doc as HD
        _BUILD.append(HB.ensure_build(HD.load()))
    fp = json.load(open(os.path.join(_BUILD[0], f"{fid}.floorplan.json"), encoding="utf-8"))
    f = [x for x in fp["fixtures"] if x["id"] == name][0]
    xs = [p[0] for p in f["points"]]; ys = [p[1] for p in f["points"]]
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)


# Survey rounds the fit leaves out, and why. Until 2026-09-25 this was done by leaving their files out of the
# engine's build copy on the server, which nothing recorded; now it is here.
# RP_SKIP_LABELS adds labels; RP_KEEP_LABELS takes labels back out (to study a skipped round).
SKIP_ROUNDS = {
    "round4_pair_10cm": "tag-pair experiment (10 cm apart): near-identical events at one spot bend the wall fit",
    "round5_pair_raised": "tag-pair experiment (raised): near-identical events at one spot bend the wall fit",
}


def skipped_rounds():
    def env(k):
        return {x for x in os.environ.get(k, "").split(",") if x}
    return (set(SKIP_ROUNDS) | env("RP_SKIP_LABELS")) - env("RP_KEEP_LABELS")


def rawcap_obs(fn):
    d = json.load(open(fn))
    obs = {}
    for nm, s in d["raw"].items():
        v = [r for _, r in s if isinstance(r, (int, float))]
        if v:
            obs[nm] = dict(med=float(st.median(v)), n=len(v))
    return obs, d.get("polls")


def fresh_dir():
    """The fresh-start folder (solver/fresh_data.py) when RP_FRESH names one, relative to the repo root; else None."""
    d = os.environ.get("RP_FRESH")
    return None if not d else (d if os.path.isabs(d) else os.path.join(HERE, "..", d))


def events(house):
    # Fresh start (Nick 2026-09-25): only data captured on today's proxy layout; see solver/fresh_data.py.
    if fresh_dir():
        import fresh_data
        return fresh_data.fresh_events(house, fresh_dir())
    # Nick's survey from before the fresh start (solver/legacy_survey.py: his house's measured spots, his devices),
    # where the installation has it; a new household has only its Calibrate-tab rounds.
    try:
        import legacy_survey as LS
    except ImportError:
        LS = None
    E = LS.events(house) if LS is not None and LS.available() else []

    # ---- rounds from the in-app Calibrate tab (solver/calib_loader.py) ---------------------
    E.extend(CL.calib_events(house))

    for e in E:
        x, y, z, _ = house.nudge(e["xy"][0], e["xy"][1], house.Z[e["fi"]] + e["h"], room=e["room"])
        e["pos"] = (x, y, z)
    return E


def epoch_kname(s, t, bounds):
    """Level name for scanner s at capture time t: the current radio, or '(old1)' for the state before
    the most recent change, '(old2)' for the one before that, ..."""
    n = sum(1 for b in bounds.get(s, ()) if t < b)
    return s if n == 0 else f"{s} (old{n})"


def fit_at(F, t, bounds):
    """A view of fit F with each scanner's level as it was at capture time t. Held-out scoring of an old
    capture must use the radio state of that day, not the current one (a re-oriented proxy can read
    7 dB differently); the live engine only ever predicts now and uses F as is."""
    import copy
    G = copy.copy(F)
    G.K = dict(F.K)
    for s in list(F.K):
        k = epoch_kname(s, t, bounds)
        if k != s and k in F.K:
            G.K[s] = F.K[k]
    return G


def epoch_names(bounds):
    return [f"{s} (old{i})" for s, bs in bounds.items() for i in range(1, len(bs) + 1)]


def without_echo(S, on):
    """RP_NO_ECHO=1 (experiment): the Echo Shows out as receivers - out of the fit and localisation, not merely
    silent (a silent receiver is censored evidence of distance)."""
    return {k: v for k, v in S.items() if not M.is_echo(k)} if on else S


NO_ECHO = os.environ.get("RP_NO_ECHO", "0") == "1"


def is_wap_event(e):
    """A WAP beacon (a ceiling AP) as the transmitter: the old survey's 'wap...' ids or a fresh anchor in a 'wapN'
    group (fresh_data.truth_table)."""
    return str(e.get("id", "")).startswith("wap") or str(e.get("group", "")).startswith("wap")


class Data:
    def __init__(self):
        self.house = g.House()
        # Each capture is featurised at the position every scanner held WHEN it was taken (moved_at /
        # previous timelines, geom3d.move_timeline); the live candidate grid uses the current positions.
        self.S = without_echo(g.load_scanners(historical=True), NO_ECHO)   # every scanner before its first move
        self.S_now = without_echo(g.load_scanners(), NO_ECHO)              # geometry in effect right now
        self.scanners = list(self.S_now)
        self.moved = g.moved_scanners()
        self.move_t, self.pos = {}, {}                  # pos[s][i]: where s stood before move i; [-1] = now
        for k, mv in g.move_timelines().items():
            self.move_t[k] = [datetime.fromisoformat(w).timestamp() for w, _ in mv]
            self.pos[k] = [{**self.S_now[k], **p} for _, p in mv] + [self.S_now[k]]
        self.moved_t = {k: ts[-1] for k, ts in self.move_t.items()}
        self.E = events(self.house)
        pts = np.array([e["pos"] for e in self.E], dtype=float).reshape(-1, 3)    # (0, 3) for a house without captures
        names, self.ebank = g.feature_bank(self.house, self.S, pts)          # before every move
        _, self.ebank_now = g.feature_bank(self.house, self.S_now, pts)      # current geometry
        self.ebank_pos = {k: [self.ebank[k]] + [g.feature_bank(self.house, {k: p}, pts)[1][k] for p in ps[1:-1]]
                          + [self.ebank_now[k]] for k, ps in self.pos.items()}
        # hardware swaps / re-seats / re-orientations: rows captured before a change fit a separate
        # '<name> (oldN)' level (see geom3d.replaced_scanners); the live level comes from after the last one
        self.replaced_t = {}
        for k, whens in g.replaced_scanners().items():
            ts = []
            for w in whens:
                try:
                    ts.append(datetime.fromisoformat(w).timestamp())
                except Exception:
                    ts.append(0.0)
            self.replaced_t[k] = sorted(ts)
        self.knames = list(self.scanners) + epoch_names(self.replaced_t)
        for k, whens in g.replaced_scanners().items():
            print(f"  [replaced] {k}: {len(whens)} change(s), last {whens[-1]}; earlier captures fit '(old1..{len(whens)})'")
        self.P, self.meta = self.house.candidates()
        self.cbank = self._prep_cand(g.cached_bank(self.house, self.S_now, self.P, "cand")[1])
        # Validating a capture taken before a move has to score it on the geometry of that day: one
        # candidate bank per combination of scanner positions, built on first use (see bank_at)
        self._cbanks = {}
        for k, ps in self.pos.items():
            hops = " -> ".join(f"({p['x']:.2f},{p['y']:.2f},z{p['z_abs']:.2f})" for p in ps)
            print(f"  [moved] {k}: {hops}  (moves at {', '.join(self.moved[k])})")
        # All nine scanners are indoors, so any k5 on an indoor point is a corner-clipping artifact
        # of straight-line ray-marching, not a real wall. Measured: 18 of 204 training rows carried
        # a spurious k5, every one indoor-to-indoor, which was pinning W5 at 0.
        if os.environ.get("RP_NOEXT", "1") == "1":            # candidate banks: _prep_cand
            for s in self.scanners:
                self.ebank[s]["k5"][:] = 0.0          # every survey point is indoors
                self.ebank_now[s]["k5"][:] = 0.0
            for bl in self.ebank_pos.values():
                for b in bl:
                    b["k5"][:] = 0.0

        for s in self.scanners:                       # receiver family flag for the Echo-slope arms
            e = 1.0 if "Show" in s else 0.0
            self.ebank[s]["echo"] = np.full(len(pts), e)
            self.ebank_now[s]["echo"] = np.full(len(pts), e)
            for b in self.ebank_pos.get(s, ()):
                b["echo"] = np.full(len(pts), e)

    def _pos_index(self, scanner, t):
        """Index into pos[scanner] of the position held at time t (0 = before the first move)."""
        return bisect.bisect_right(self.move_t[scanner], t)

    def _pos_at(self, scanner, t):
        """Where this scanner stood at time t."""
        return self.pos[scanner][self._pos_index(scanner, t)] if scanner in self.move_t else self.S_now[scanner]

    def _ebank_at(self, scanner, t):
        """This scanner's event features at the position it held at time t."""
        if scanner not in self.move_t:
            return self.ebank_now[scanner]
        return self.ebank_pos[scanner][self._pos_index(scanner, t)]

    def _now_at(self, scanner, event):
        """Was this scanner already at its current position when this event was captured?"""
        return scanner in self.move_t and self._pos_index(scanner, event.get("t") or 0.0) == len(self.move_t[scanner])

    def _prep_cand(self, bank):
        """The k5 corner-clip mask and receiver-family flag (see __init__), for a candidate bank."""
        if os.environ.get("RP_NOEXT", "1") == "1":
            keep = np.array([self.house.room_kind(m[0], m[1]) == "outdoor" for m in self.meta], dtype=float)
            for s in self.scanners:
                bank[s]["k5"] = bank[s]["k5"] * keep
        for s in self.scanners:
            bank[s]["echo"] = np.full(len(self.P), 1.0 if "Show" in s else 0.0)
        return bank

    def bank_at(self, t):
        """Candidate bank for the geometry at time t: every moved scanner at the position it held then."""
        key = tuple(sorted((k, i) for k in self.move_t for i in [self._pos_index(k, t)] if i < len(self.move_t[k])))
        if not key:
            return self.cbank
        if key not in self._cbanks:
            S = {k: self._pos_at(k, t) for k in self.scanners}
            self._cbanks[key] = self._prep_cand(g.cached_bank(self.house, S, self.P, "cand")[1])
        return self._cbanks[key]

    @property
    def cbank_hist(self):
        """Geometry before every recorded move."""
        return self.bank_at(float("-inf"))

    def bank_for(self, event):
        """Candidate bank matching an event's capture time."""
        return self.bank_at(event.get("t") or 0.0)

    def rows(self, groups):
        y, ki, oi, feats, anchors = [], [], [], {k: [] for k in self.ebank[self.scanners[0]]}, []
        feats["wap_up"] = []
        for i, e in enumerate(self.E):
            if e["group"] not in groups or not CL.trainable(e):
                continue               # validation / co-location rounds are never trained on
            det, _ = M.split_obs(e["obs"], 10 if e["kind"] == "phone" else 5)
            for s, med in det.items():
                if s not in self.scanners:
                    continue
                og = e.get("ogroup", e["id"])
                if e["kind"] == "anchor" and og not in anchors:
                    anchors.append(og)
                t = e.get("t") or 0.0
                bs = self._ebank_at(s, t)
                kname = epoch_kname(s, e.get("t") or 0.0, self.replaced_t)
                y.append(med); ki.append(self.knames.index(kname))
                oi.append(anchors.index(og) if e["kind"] == "anchor" else -1)
                for k in feats:
                    if k == "wap_up":
                        up = WAPUP and is_wap_event(e) and self._pos_at(s, t)["z_abs"] > e["pos"][2]
                        feats[k].append(1.0 if up else 0.0)
                    else:
                        feats[k].append(bs[k][i])
        return (np.array(y), np.array(ki), np.array(oi),
                {k: np.array(v) for k, v in feats.items()}, anchors)

    def fit_all(self, groups, models=None):
        """Each model fitted to the groups' rows; with nothing to fit (a house without captures, or none of its
        events trainable), the default model (spec 2026-10-01-installable-and-published, A2)."""
        y, ki, oi, fa, anchors = self.rows(groups)
        if not len(y):
            defaults = M.load_defaults()
            return {m: M.default_fit(m, self.knames, defaults) for m in (models or PHYS_RUN)}
        return {m: M.fit(m, y, ki, oi, fa, self.knames, anchors) for m in (models or PHYS_RUN)}


# v2: Walls/CappedWalls dropped (1-2/9, confident calls all wrong); Echo-slope arms dropped
# (fitted dne ~ 0 with opposite signs, no out-of-sample gain). Set RP_V2=0 to reproduce v1.
PHYS_RUN = (["FreeSpace", "DominantPath"] if V2 else M.PHYS)
MODELS_RUN = ["Bermuda", "Bermuda+offsets"] + PHYS_RUN


def bermuda(D, obs, bias=None, min_n=10, dead=()):
    det, _ = M.split_obs(obs, min_n)
    det = {s: v for s, v in det.items() if s not in set(dead) and s in D.S}   # readings from receivers the model has
    if not det:
        return None
    score = {s: v - (bias or {}).get(s, 0.0) for s, v in det.items()}
    s = max(score, key=score.get)
    v = D.S[s]
    return dict(floor=D.house.floor_of_z(v["z_abs"]), room=v["room"], xy=(v["x"], v["y"]),
                p_room=float("nan"), p_floor=float("nan"), verdict="CALL", r68=float("nan"), top=[], via=s)


def absent_at(t):
    """Scanners not installed yet at time t (registry added_at): silent because absent, not because far."""
    if t is None:
        return set()
    return {s for s, t0 in g.added_scanners().items() if t < t0}


def predict_all(D, fits, obs, kind, dead=(), bank=None, t=None):
    min_n, off_sd = (10, 4.0) if kind == "phone" else (5, 12.0)
    dead = set(dead or ()) | absent_at(t)
    if t is not None and getattr(D, "replaced_t", None):
        fits = {m: fit_at(F, t, D.replaced_t) for m, F in fits.items()}
    out = {"Bermuda": bermuda(D, obs, None, min_n, dead),
           "Bermuda+offsets": bermuda(D, obs, fits["FreeSpace"].bias(), min_n, dead)}
    for m in PHYS_RUN:
        R = M.localise(m, fits[m], obs, D.scanners, bank if bank is not None else D.cbank_hist,
                   D.meta, D.P, min_n=min_n, off_sd=off_sd,
                       dead=dead)
        out[m] = M.summarise(R, D.house)
    return out


def score(pred, truth_xy, truth_fi, truth_room):
    fl = pred["floor"] if "map_floor" not in pred else pred["room_floor"]
    err = math.hypot(pred["xy"][0] - truth_xy[0], pred["xy"][1] - truth_xy[1])
    floor_ok = fl == truth_fi
    room_ok = floor_ok and pred["room"] == truth_room
    return dict(err=err, floor_ok=floor_ok, room_ok=room_ok,
                passed=floor_ok and (room_ok or err <= 1.5))


def cv():
    t0 = time.time()
    D = Data()
    groups = sorted({e["group"] for e in D.E})
    print(f"data: {len(D.E)} events in {len(groups)} location groups, {len(D.scanners)} scanners, "
          f"{len(D.P)} candidate points  ({time.time()-t0:.1f}s)\n")
    full = D.fit_all(groups)
    print("=== in-sample fit, all data ===")
    for m, F in full.items():
        ps = "  ".join(f"{k}={v:.2f}" for k, v in F.p.items())
        print(f"  {m:13} rms {F.rms:4.1f}  mad {F.mad:4.1f} dB   {ps}")
    ref = os.environ.get("RP_BIAS_REF") or "the first ESP32 receiver"
    print(f"\n  per-scanner bias vs {ref} (Bermuda rssi_offset would be the NEGATIVE):")
    for s in D.scanners:
        print(f"    {s:24} " + "  ".join(f"{m[:10]}:{full[m].bias()[s]:+5.1f}" for m in PHYS_RUN))
    print("\n=== leave-one-LOCATION-out (model never saw the held-out spot) ===")
    tab = {m: [] for m in MODELS_RUN}
    for gname in groups:
        fits = D.fit_all([x for x in groups if x != gname])
        for e in [x for x in D.E if x["group"] == gname and not x.get("co_location")]:
            preds = predict_all(D, fits, e["obs"], e["kind"], bank=D.bank_for(e), t=e.get("t") or 0.0)
            print(f"\n  held out: {e['id']:15} truth = {e['room']} / {FLOORNAME[e['fi']]}  ({e['kind']})")
            for m in MODELS_RUN:
                p = preds[m]
                if p is None:
                    print(f"    {m:16} no detections"); continue
                sc = score(p, e["pos"][:2], e["fi"], e["room"])
                tab[m].append(sc)
                conf = "" if math.isnan(p["p_room"]) else f"p={p['p_room']:.2f} r68={p['r68']:.1f}m {p['verdict']}"
                via = f"  [strongest: {p['via']}]" if "via" in p else ""
                print(f"    {m:16} {str(p['room'])[:16]:16} {FLOORNAME[p['floor'] if 'room_floor' not in p else p['room_floor']]:9} "
                      f"err {sc['err']:4.1f} m  {'ROOM OK' if sc['room_ok'] else ('floor ok' if sc['floor_ok'] else 'WRONG FLOOR'):11} {conf}{via}")
    print("\n=== summary across held-out events ===")
    print(f"  {'model':16} {'room':>6} {'floor':>6} {'pass':>6} {'median err':>11}")
    for m in MODELS_RUN:
        r = tab[m]
        if not r: continue
        print(f"  {m:16} {sum(x['room_ok'] for x in r):3d}/{len(r)} {sum(x['floor_ok'] for x in r):3d}/{len(r)} "
              f"{sum(x['passed'] for x in r):3d}/{len(r)} {st.median(x['err'] for x in r):9.1f} m")
    print(f"\n  ({time.time()-t0:.0f}s)")


def defaults_score():
    """Every event located on the default model alone (models.default_fit, no fitting at all): what a house with
    receivers and no calibration captures can expect. The defaults come from this house's own fit, so on its data
    this is optimistic for another house."""
    D = Data()
    defaults = M.load_defaults()
    fits = {m: M.default_fit(m, D.knames, defaults) for m in PHYS_RUN}
    tab = {m: [] for m in PHYS_RUN}
    for e in [x for x in D.E if not x.get("co_location")]:
        preds = predict_all(D, fits, e["obs"], e["kind"], bank=D.bank_for(e), t=e.get("t") or 0.0)
        for m in PHYS_RUN:
            if preds[m] is not None:
                tab[m].append(score(preds[m], e["pos"][:2], e["fi"], e["room"]))
    print(f"=== {len(D.E)} events on the default model (no fitting) ===")
    print(f"  {'model':16} {'room':>6} {'floor':>6} {'pass':>6} {'median err':>11}")
    out = {}
    for m in PHYS_RUN:
        r = tab[m]
        if not r:
            continue
        out[m] = dict(room=sum(x["room_ok"] for x in r), floor=sum(x["floor_ok"] for x in r), n=len(r),
                      err=st.median(x["err"] for x in r))
        print(f"  {m:16} {out[m]['room']:3d}/{len(r)} {out[m]['floor']:3d}/{len(r)} "
              f"{sum(x['passed'] for x in r):3d}/{len(r)} {out[m]['err']:9.1f} m")
    return out


def spots_curve(ks=(1, 3, 10, 25), draws=3, n_test=10, seed=0, model="DominantPath"):
    """A new household's first calibration spots against the default model (plan 2026-10-01-a4): this installation's
    trainable locations stand in for a household's (its rounds are mostly tag co-location, not positions). n_test
    locations are held out; for each k, `draws` random picks of k other locations are fitted alone, and the fit and
    the defaults are scored on the held-out locations' events."""
    import random
    D = Data()
    groups = sorted({e["group"] for e in D.E if CL.trainable(e)})
    rng = random.Random(seed)
    test_g = set(rng.sample(groups, n_test))
    pool = [g_ for g_ in groups if g_ not in test_g]
    test = [e for e in D.E if e["group"] in test_g and CL.trainable(e)]
    print(f"=== {len(groups)} locations: {n_test} held out ({len(test)} events), fits on k of the other {len(pool)} ===")

    def tally(fits):
        r = []
        for e in test:
            p = predict_all(D, fits, e["obs"], e["kind"], bank=D.bank_for(e), t=e.get("t") or 0.0)[model]
            if p is not None:
                r.append(score(p, e["pos"][:2], e["fi"], e["room"]))
        return dict(room=sum(x["room_ok"] for x in r), floor=sum(x["floor_ok"] for x in r), n=len(r),
                    err=st.median(x["err"] for x in r) if r else float("nan"))

    def show(label, t):
        print(f"  {label:26} {t['room']:3d}/{t['n']} {t['floor']:3d}/{t['n']} {t['err']:6.1f} m", flush=True)

    defaults = M.load_defaults()
    print(f"  {'':26} {'room':>6} {'floor':>6} {'median err':>9}")
    out = {"defaults": tally({m: M.default_fit(m, D.knames, defaults) for m in PHYS_RUN})}
    show("defaults", out["defaults"])
    for k in ks:
        for d in range(draws if k < len(pool) else 1):
            pick = sorted(rng.sample(pool, k))
            out[f"{k}/{d}"] = t = tally(D.fit_all(pick))
            show(f"fit on {k} spot{'s' if k > 1 else ''} (draw {d + 1})", t)
    return out


def dead_scanners(fn, limit=60.0):
    """Scanners whose newest advert from ANY device was > limit seconds old at capture start or end."""
    h = json.load(open(fn)).get("health") or {}
    dead = set()
    for phase in ("start", "end"):
        for s, age in (h.get(phase) or {}).items():
            if age is None or age > limit:
                dead.add(s)
    return sorted(dead), bool(h)


def locate(fn):
    D = Data()
    obs, polls = rawcap_obs(fn)
    groups = sorted({e["group"] for e in D.E})
    fits = D.fit_all(groups)
    dead, checked = dead_scanners(fn)
    if not checked:
        print("WARNING: capture has no scanner-health record - a dead scanner would be read as 'far away'")
    elif dead:
        print(f"HEALTH: dropping scanners that heard nothing during the capture: {', '.join(dead)}")
    else:
        print("HEALTH: all scanners alive during the capture")
    preds = predict_all(D, fits, obs, "phone", dead=dead)
    print(f"capture {os.path.basename(fn)}: " + ", ".join(f"{s.split()[0]} {o['med']:.0f}/{o['n']}" for s, o in obs.items()))
    print(f"\n{'model':16} {'floor':9} {'room':18} {'p(room)':>7} {'p(floor)':>8} {'r68':>6}  verdict   (runner-up)")
    import hashlib
    code = {f: hashlib.sha1(open(os.path.join(HERE, f), "rb").read()).hexdigest()[:10]
            for f in ("geom3d.py", "models.py", "bakeoff.py")}
    rec = dict(capture=fn, made=time.strftime("%Y-%m-%d %H:%M:%S"), predictions={},
               dead_scanners=dead,
               frozen=dict(FIT_LOSS=M.FIT_LOSS, NU=M.NU, code=code,
                           training=[e["id"] for e in D.E]))
    for m in MODELS_RUN:
        p = preds[m]
        if p is None:
            continue
        fl = p["floor"] if "room_floor" not in p else p["room_floor"]
        ru = ""
        if p.get("top") and len(p["top"]) > 1:
            (f2, r2), pp = p["top"][1]; ru = f"{r2} {pp:.2f}"
        pr = "" if math.isnan(p["p_room"]) else f"{p['p_room']:.2f}"
        pf = "" if math.isnan(p["p_floor"]) else f"{p['p_floor']:.2f}"
        r6 = "" if math.isnan(p["r68"]) else f"{p['r68']:.1f}m"
        print(f"{m:16} {FLOORNAME[fl]:9} {str(p['room'])[:18]:18} {pr:>7} {pf:>8} {r6:>6}  {p['verdict']:8}  {ru}")
        rec["predictions"][m] = {k: (v if not isinstance(v, (np.floating, np.integer)) else float(v))
                                 for k, v in p.items() if k in ("floor", "room_floor", "room", "p_room",
                                                                "p_floor", "xy", "r68", "verdict", "via")}
    out = fn.replace(".json", "_PREDICTIONS.json")
    json.dump(rec, open(out, "w"), indent=1, default=float)
    print(f"\npredictions written BEFORE reveal: {out}  ({rec['made']})")


def score_cli(pred_fn, x, y, floor, room=None):
    D = Data()
    fi = {"1": 0, "2": 1, "main": 1, "3": 2}[str(floor)]
    room = room or D.house.room_at(fi, x, y)
    rec = json.load(open(pred_fn))
    print(f"truth: ({x}, {y}) {FLOORNAME[fi]} / {room}   predictions made {rec['made']}\n")
    for m, p in rec["predictions"].items():
        p = dict(p); p["floor"] = p.get("room_floor", p["floor"])
        sc = score(p, (x, y), fi, room)
        print(f"  {m:16} {'PASS' if sc['passed'] else 'fail':5} room {'ok' if sc['room_ok'] else 'X':3} "
              f"floor {'ok' if sc['floor_ok'] else 'X':3} err {sc['err']:4.1f} m   ({p['verdict']})")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "cv"
    if cmd == "cv":
        cv()
    elif cmd == "defaults":
        defaults_score()
    elif cmd == "spots":
        spots_curve()
    elif cmd == "locate":
        locate(sys.argv[2])
    elif cmd == "score":
        score_cli(sys.argv[2], float(sys.argv[3]), float(sys.argv[4]), sys.argv[5],
                  sys.argv[6] if len(sys.argv) > 6 else None)
