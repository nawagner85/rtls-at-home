"""Onboarding (spec 3.4): a fast locator for devices the engine doesn't track yet, the panel's lease, and a live check
of the locator against the tracker. Candidates are for choosing a tag, not for tracking one.

The locator uses the engine's own model and candidate points (every point at three heights, summed per 0.25 m cell,
exactly as tracking does), with a transmit-power prior for household devices rather than the survey tags. The house
model has no outside: a device that only an implausibly loud transmitter could explain, or that fits poorly, is
called "probably outside".

History (2026-09-25): a coarse grid reaching 50 ft outside (one height per floor, outside cells extrapolated past the
exterior wall, prior centred on the survey tags) passed its synthetic tests and failed on live data - ceiling lights
in one room came out 8-12 m outside on the top floor, and 52 of 69 real candidates were called outside.
"""
import math
import os
import threading
import time

import numpy as np
from scipy.special import log_ndtr

import identify as ID
import models as M

# Transmit power relative to the survey tags. Live census 2026-09-25: household gadgets that land in rooms imply
# -10 to -19 dB; fits needing +1 to +9 dB piled into the far corner of the house (see solver/EXPERIMENTS.md).
TX_MEAN = -12.0             # dB: the prior's centre for an unknown household device
TX_SD = 8.0                 # dB
TX_LOUD = 3.0               # dB: a device needing a louder transmitter than this is probably outside
# dB more on the default model (A5 clean room): its receiver levels are the developer's (his proxies sd 1.4 dB, his
# Echo Shows 2.9, up to 6 off their kind's level), not the house's own, and a household's tags may be hotter than his
# survey tags - a household's own tag hidden as "outside" is worse than a neighbour's shown
DEFAULT_TX_SLACK = 6.0
FIT_RMS = 7.0               # dB: ...and so is one whose best in-house fit is worse than this (rms over its proxies)
MIN_PROXIES, MIN_SAMPLES = 2, 2


def marginal_loglik(mu, sigma, obs_idx, obs_rssi, cen_idx, off_sd=TX_SD, off_mean=TX_MEAN):
    """Log-likelihood of every point for one device. Gaussian readings with an unknown transmitter offset
    o ~ N(off_mean, off_sd^2) integrated out in closed form; a proxy that heard nothing adds a censored term at the
    offset's posterior mean. mu: (points, scanners) predicted dB; sigma: (scanners,)."""
    w = 1.0 / sigma[obs_idx] ** 2
    d = (obs_rssi - off_mean)[None, :] - mu[:, obs_idx]
    swd = (d * w).sum(axis=1)
    prec = w.sum() + 1.0 / off_sd ** 2
    ll = -0.5 * ((d * d * w).sum(axis=1) - swd * swd / prec)
    if len(cen_idx):
        o = off_mean + swd / prec
        z = (M.CENSOR_AT - mu[:, cen_idx] - o[:, None]) / sigma[cen_idx][None, :]
        ll = ll + log_ndtr(z).sum(axis=1)
    return ll


def looks_outside(tx, rms, loud=TX_LOUD):
    """Probably not in the house: only an implausibly loud transmitter explains it, or the house fits it poorly."""
    return tx > loud or rms > FIT_RMS


class HouseLocator:
    def __init__(self, eng, model=None):
        m = model or ("DominantPath" if "DominantPath" in eng.models else eng.models[0])
        self.eng = eng
        self.MU = eng.MU[m].T                                    # (points, scanners)
        self.scanners = list(eng.scanners)
        self.idx = {s: j for j, s in enumerate(self.scanners)}
        self.sigma = np.array([math.sqrt(eng.sigma[m] ** 2 + M.extra_sd(s) ** 2) for s in self.scanners])
        self.room = [eng.cell_room[c] for c in eng.gidx]         # per point
        on_defaults = bool((getattr(eng, "trained", None) or {}).get("default"))
        self.loud = TX_LOUD + (DEFAULT_TX_SLACK if on_defaults else 0.0)

    def locate(self, per_scanner, dead=()):
        """per_scanner: {scanner name: (median rssi, samples)}. None unless MIN_PROXIES live proxies have at least
        MIN_SAMPLES samples each."""
        dead = set(dead)
        det = [(self.idx[s], float(v[0])) for s, v in per_scanner.items()
               if s in self.idx and s not in dead and int(v[1]) >= MIN_SAMPLES]
        if len(det) < MIN_PROXIES:
            return None
        obs_idx = np.array([j for j, _ in det], dtype=int)
        obs_rssi = np.array([r for _, r in det])
        cen_idx = np.array([j for s, j in self.idx.items() if s not in per_scanner and s not in dead], dtype=int)
        ll = marginal_loglik(self.MU, self.sigma, obs_idx, obs_rssi, cen_idx)
        j = int(np.argmax(ll))
        w = 1.0 / self.sigma[obs_idx] ** 2
        d = obs_rssi - TX_MEAN - self.MU[j, obs_idx]
        o = float((d * w).sum() / (w.sum() + 1.0 / TX_SD ** 2))
        tx, rms = TX_MEAN + o, float(np.sqrt(np.mean((d - o) ** 2)))
        post = np.bincount(self.eng.gidx, weights=np.exp(ll - ll.max()), minlength=self.eng.nC)
        S = self.eng.summarise(post / post.sum())
        return dict(floor=int(S["map_floor"]), floor_name=S["floor_name"], x=round(S["xy"][0], 2),
                    y=round(S["xy"][1], 2), room=S["room"], p_room=round(S["p_room"], 2), r68=round(S["r68"], 1),
                    outside=looks_outside(tx, rms, self.loud), tx=round(tx, 1), fit_rms=round(rms, 1))


LEASE_S = float(os.environ.get("RTLS_ONBOARD_LEASE", "30"))   # s the panel keeps onboarding on; tests shorten it
MEMORY_S = 30.0            # s: each proxy's readings of a candidate are averaged over about this long (Nick 2026-09-25)
SETTLE_S = 20.0            # s of history before a candidate's position counts as settled
FORGET_S = 60.0            # s unheard before a candidate's history is dropped
# s after the panel opens before a census without detail means an old bridge: the census the engine holds when the
# panel opens is the plain one, and the bridge only switches on its next census (<= 10 s). Tests shorten it.
DETAIL_GRACE = float(os.environ.get("RTLS_ONBOARD_DETAIL_GRACE", "15"))


class Onboarding:
    """The panel's lease, and the candidates located from the bridge's detailed census. Candidates live in memory
    only and are never written to a session file: they include the neighbours' devices. Locating runs in the
    viewer's own GET, once per new census, so the ingest reply never waits for it."""

    def __init__(self, source, devices, dead, grid_factory, log=print, clock=time.time, background=True,
                 tracked_obs=None):
        """tracked_obs(): [{key, name, per, room, floor}] - tracked devices' own readings and the tracker's answer,
        to score the locator on real data (the `check` in every view)."""
        self.src, self.devs, self.dead, self.log, self.clock = source, devices, dead, log, clock
        self.tracked_obs = tracked_obs
        self.grid, self.error, self.lease_until, self.lease_started = None, None, float("-inf"), None
        self.result = dict(at=None, candidates=[], one_proxy=0, plain=False, check=None)
        self._census_at = None
        self._hist = {}            # candidate key -> {first, last, per: {scanner: [average, samples, at]}}
        self._lock = threading.Lock()
        if background:
            threading.Thread(target=self._build, args=(grid_factory,), daemon=True).start()
        else:
            self._build(grid_factory)

    def _build(self, factory):
        t = time.time()
        try:
            grid = factory()
            self.log(f"onboarding locator ready in {time.time() - t:.1f}s: {len(getattr(grid, 'MU', ()))} points")
            self.grid = grid
        except Exception as e:                  # onboarding off, tracking unaffected
            self.error = f"{type(e).__name__}: {e}"
            self.log("onboarding disabled:", self.error)

    def active(self):
        return self.clock() < self.lease_until

    def view(self):
        """GET /api/onboarding: renews the lease and returns the latest candidates."""
        now = self.clock()
        if not self.active():
            self.lease_started = now                 # the panel (re)opened
        self.lease_until = now + LEASE_S
        with self._lock:
            at = getattr(self.src, "census_at", None)
            if self.grid is not None and at is not None and at != self._census_at:
                self._census_at = at
                self.result = self.locate_rows(list(self.src.census), at)
        plain = self.result["plain"]
        outdated = plain and now - self.lease_started > DETAIL_GRACE
        status = ("error" if self.error else "building" if self.grid is None else
                  "waiting" if plain and not outdated else "ready")
        out = {k: v for k, v in self.result.items() if k != "plain"}
        return dict(status=status, error=self.error, lease_s=LEASE_S, bridge_outdated=outdated, **out)

    def _smoothed(self, key, per, at):
        """Fold this census into the candidate's running per-proxy averages (time constant MEMORY_S). A proxy that
        missed this census keeps its average for up to MEMORY_S. Returns (averaged readings, still settling, rises):
        rises are this census's median minus the average before it, per proxy that has one - a device held next to
        a proxy jumps there (Nick 2026-09-27: telling devices apart without their MAC)."""
        h = self._hist.get(key)
        if h is None or at - h["last"] > FORGET_S:
            h = self._hist[key] = dict(first=at, last=at, per={})
        a = 1.0 - math.exp(-max(at - h["last"], 0.0) / MEMORY_S)
        rises = {s: med - h["per"][s][0] for s, (med, n) in per.items() if s in h["per"] and at > h["per"][s][2]}
        for s, (med, n) in per.items():
            old = h["per"].get(s)
            h["per"][s] = [med, n, at] if old is None else [old[0] + a * (med - old[0]), max(n, old[1]), at]
        h["per"] = {s: v for s, v in h["per"].items() if at - v[2] <= MEMORY_S}
        h["last"] = at
        return {s: (v[0], v[1]) for s, v in h["per"].items()}, at - h["first"] < SETTLE_S, rises

    def locate_rows(self, rows, at):
        known, dead = set(self.devs.devices), set(self.dead())
        out, one = [], 0
        detailed = [r for r in rows if "per_scanner" in r]
        for r in detailed:
            keys = list(r.get("keys") or [])
            if not keys or any(k in known for k in keys):
                continue
            key = keys[-1] if r.get("ibeacon") and len(keys) > 1 else keys[0]
            per = {}
            for addr, med, n in r["per_scanner"]:
                name = self.src.scanner_name(addr)
                if name:
                    per[name] = (float(med), int(n))
            now = dict(per)                  # this census alone: which proxy hears it best right now
            per, settling, rises = self._smoothed(key, per, at)
            loc = self.grid.locate(per, dead)
            if loc is None:
                one += 1
                continue
            top = max(rises.items(), key=lambda kv: kv[1]) if rises else None
            out.append(dict(key=key, keys=keys, name=r.get("name"), ibeacon=bool(r.get("ibeacon")),
                            rssi=r.get("rssi"), proxies=len(per), first_seen=r.get("first_seen"),
                            addr_type=r.get("addr_type"), settling=settling, adv_tx=r.get("tx"),
                            nearest=max(now or per, key=lambda s: (now or per)[s][0]),
                            rise=dict(db=round(top[1], 1), proxy=top[0]) if top else None,
                            **ID.describe(r), **loc))
        self._hist = {k: h for k, h in self._hist.items() if at - h["last"] <= FORGET_S}
        return dict(at=at, candidates=out, one_proxy=one, plain=bool(rows) and not detailed,
                    check=self._check(dead))

    def _check(self, dead):
        """The locator on the tracked devices' own readings, against where the tracker puts them."""
        if self.tracked_obs is None:
            return None
        rows = []
        for t in self.tracked_obs():
            loc = self.grid.locate(t["per"], dead)
            if loc is None:
                continue
            rows.append(dict(name=t["name"], tracker=f"{t['room']} / {t['floor']}",
                             locator=f"{loc['room']} / {loc['floor_name']}", outside=bool(loc.get("outside")),
                             same_room=loc["room"] == t["room"], same_floor=loc["floor_name"] == t["floor"]))
        return dict(n=len(rows), same_room=sum(r["same_room"] for r in rows),
                    same_floor=sum(r["same_floor"] for r in rows), rows=rows)
