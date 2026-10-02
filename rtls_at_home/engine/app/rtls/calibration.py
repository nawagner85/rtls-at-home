"""Running calibration (docs/specs/2026-09-27-running-calibration-design.md).

ReceiverLevels: each receiver's level against the fixed anchors, followed slowly, and a change that holds recorded as a
level event (sessions/level_events.json) so it can become a `replaced_at` stamp and the next fit splits the epoch
instead of bending its physics to absorb it (2026-09-27: a proxy went 8-11 dB weaker with no swap).

DeviceOffsets: each device's transmit offset, learned from the tracker's own per-window offset estimate only when the
device is still, the room filter is confident and the one-window call agrees with it - a device placed wrong with
confidence would otherwise lock its error in. Bounded, slow, persisted (sessions/device_offsets.json).
"""
import json
import math
import os
import time


def _write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def shrink(c, sigma):
    """Noise-level values shrink to ~0, large ones pass (the live anchor correction's rule)."""
    return c * (c * c) / (c * c + sigma * sigma)


class ReceiverLevels:
    """The learned level can replace the live (instant) anchor correction as a filter on it, or be blended with it
    (Nick 2026-09-27): blend is the instant correction's weight (0: learned only). fast_tau_s turns on the adaptive
    filter: a fast level alongside the slow one; when they stay step_db apart for step_hold_s, the slow level jumps to
    the fast one - noise is ridden out, a real step (a board knocked, a car moved) is adopted in minutes."""

    def __init__(self, tau_s=600.0, event_db=4.0, hold_s=1800.0, path=None, sigma=1.5, sigma_esp=3.0, blend=0.0,
                 fast_tau_s=None, step_db=2.0, step_hold_s=120.0):
        self.tau, self.event_db, self.hold, self.path = tau_s, event_db, hold_s, path
        self.sigma, self.sigma_esp = sigma, sigma_esp
        self.blend, self.fast_tau, self.step_db, self.step_hold = blend, fast_tau_s, step_db, step_hold_s
        self.level, self.last, self.since, self.armed = {}, {}, {}, {}
        self.fast, self.step_since = {}, {}
        self.events = []
        if path and os.path.exists(path):
            try:
                self.events = list(json.load(open(path, encoding="utf-8")).get("events") or [])
            except (OSError, ValueError):
                self.events = []

    def update(self, deltas, now):
        """deltas: {receiver: median anchor delta in dB, or None when this tick's anchors disagree}."""
        for s, v in deltas.items():
            if v is None:
                continue
            if s not in self.level:
                self.level[s] = self.fast[s] = float(v)
            else:
                dt = max(now - self.last[s], 0.0)
                self.level[s] += (1.0 - math.exp(-dt / self.tau)) * (float(v) - self.level[s])
                if self.fast_tau:
                    self.fast[s] += (1.0 - math.exp(-dt / self.fast_tau)) * (float(v) - self.fast[s])
                    if abs(self.fast[s] - self.level[s]) >= self.step_db:
                        self.step_since.setdefault(s, now)
                        if now - self.step_since[s] >= self.step_hold:
                            self.level[s] = self.fast[s]
                            self.step_since.pop(s, None)
                    else:
                        self.step_since.pop(s, None)
            self.last[s] = now
            self._check(s, now)

    def _check(self, s, now):
        lv = self.level[s]
        if abs(lv) >= self.event_db:
            self.since.setdefault(s, now)
            if self.armed.get(s, True) and now - self.since[s] >= self.hold:
                self.armed[s] = False
                self.events.append(dict(scanner=s, since=self.since[s], level_db=round(lv, 1), detected=now,
                                        since_iso=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.since[s]))))
                if self.path:
                    _write(self.path, dict(events=self.events))
        else:
            self.since.pop(s, None)
            if abs(lv) < self.event_db / 2.0:
                self.armed[s] = True

    def correction(self, is_echo=lambda s: False, instant=None):
        """{receiver: dB to add to its expected level}: the running level, shrunk like the live correction, or with
        blend > 0 mixed with the instant correction (a receiver with no level yet takes the instant value)."""
        learned = {s: shrink(v, self.sigma if is_echo(s) else self.sigma_esp) for s, v in self.level.items()}
        if self.blend > 0 and instant is not None:
            names = set(learned) | set(instant)
            return {s: round(self.blend * instant.get(s, 0.0) + (1.0 - self.blend) * learned.get(s, instant.get(s, 0.0)), 2)
                    for s in names}
        return {s: round(v, 2) for s, v in learned.items()}


class DeviceOffsets:
    def __init__(self, tau_s=1800.0, bound=10.0, min_p=0.8, max_motion=2.0, max_dt=60.0, path=None, init=None):
        self.tau, self.bound, self.min_p, self.max_motion, self.max_dt = tau_s, bound, min_p, max_motion, max_dt
        self.path = path
        self.means = {k: float(v) for k, v in (init or {}).items()}
        self.learned, self.last = set(), {}
        if path and os.path.exists(path):
            try:
                for k, v in (json.load(open(path, encoding="utf-8")).get("offsets") or {}).items():
                    self.means[k] = float(v)
                    self.learned.add(k)
            except (OSError, ValueError):
                pass

    def mean(self, key):
        return self.means.get(key, 0.0)

    def update(self, key, offset, now, p_room, agrees, motion_db, warming):
        """Fold one window's offset estimate in, if the tracker earned it. Returns True when it was used."""
        if (offset is None or warming or not agrees or p_room is None or p_room < self.min_p
                or motion_db is None or motion_db > self.max_motion):
            return False
        if key not in self.last:
            self.last[key] = now                 # the first accepted window only starts the clock
            return False
        dt = min(max(now - self.last[key], 0.0), self.max_dt)
        self.last[key] = now
        a = 1.0 - math.exp(-dt / self.tau)
        m = self.mean(key) + a * (float(offset) - self.mean(key))
        self.means[key] = max(-self.bound, min(self.bound, m))
        self.learned.add(key)
        return True

    def save(self):
        if self.path:
            _write(self.path, dict(offsets={k: round(self.means[k], 2) for k in sorted(self.learned)}))


MODES = ("off", "shadow", "on")


def receiver_options():
    """ReceiverLevels settings from the environment: RTLS_RECEIVER_TAU (s, the learned level's time constant),
    RTLS_RECEIVER_BLEND (0-1, the instant correction's weight), RTLS_RECEIVER_FAST_TAU (s; set = the adaptive filter)."""
    fast = os.environ.get("RTLS_RECEIVER_FAST_TAU")
    return dict(tau_s=float(os.environ.get("RTLS_RECEIVER_TAU", "600")),
                blend=float(os.environ.get("RTLS_RECEIVER_BLEND", "0")),
                fast_tau_s=float(fast) if fast else None)


def mode(env, default="shadow"):
    """off: nothing; shadow: learn and record, change nothing the tracker reports; on: apply what was learned."""
    v = os.environ.get(env, default)
    return v if v in MODES else default


def learn(offsets, key, est, motion_db, now):
    """Fold one device's primary-model estimate into its running offset; the gate is DeviceOffsets.update's. A device
    without a room filter (phones) has no one-window call to disagree with."""
    if not isinstance(est, dict):
        return False
    raw = est.get("raw")
    agrees = True if not raw else raw.get("room") == est.get("room")
    return offsets.update(key, est.get("offset"), now, p_room=est.get("p_room"), agrees=agrees,
                          motion_db=motion_db, warming=bool(est.get("warming")))


def initial_offsets(known, calib_ids):
    """known: {device key or tag id: dB}; calib_ids: {device key: tag id}. Device keys only."""
    out = {k: v for k, v in known.items() if ":" in k or "_" in k}
    out.update({key: known[tid] for key, tid in calib_ids.items() if tid in known})
    return out
