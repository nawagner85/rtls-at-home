"""In-app calibration (spec: docs/specs/2026-09-24-calibration-tab-design.md).

Rig, placement checks, sample summaries and quality rules, the capture job, room/scanner coverage,
and the queue that runs convergence checks. Everything here is plain data in, plain data out, except
CaptureJob (a thread driving the recorder on the Bermuda poller) and ConvergenceQueue (a worker
thread running solver/convergence.py as a low-priority subprocess).

On disk, under <sessions>/calib/:
  rig.json                 poles, slots, heights, snap offset, units
  rounds/<round_id>.json   one per kept round, never overwritten
  checks/<check_id>.json   one per convergence check
"""
import copy
import json
import os
import statistics as st
import tempfile
import threading
import time

IN = 0.0254
MAX_HEIGHT = 2.6
CONTEXTS = ("free", "on_surface", "inside")
UNITS = ("in", "cm")
GAP_ALLOWED_S = 5.0          # a poll interval longer than this counts as an HA gap (beyond the allowance)

DEFAULT_RIG = {
    "poles": [{"id": pid, "slots": [{"slot": "low", "tag_key": None, "height_m": 0.30},
                                    {"slot": "mid", "tag_key": None, "height_m": 1.00},
                                    {"slot": "high", "tag_key": None, "height_m": 1.70}]} for pid in ("A", "B")],
    "snap_offset_m": 12 * IN,
    "snap_radius_m": 0.6,
    "units": "in",
}


# ---- files --------------------------------------------------------------------------------
def write_json_atomic(path, obj, overwrite=True):
    """Write whole or not at all: temp file in the same folder, then rename."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    if not overwrite and os.path.exists(path):
        raise FileExistsError(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1)
        if not overwrite and os.path.exists(path):
            raise FileExistsError(path)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def new_id(prefix, t):
    return prefix + time.strftime("%Y%m%dT%H%M%S", time.gmtime(t))


# ---- rig ----------------------------------------------------------------------------------
def load_rig(calib_dir):
    try:
        rig = json.load(open(os.path.join(calib_dir, "rig.json"), encoding="utf-8"))
        if isinstance(rig, dict) and rig.get("poles"):
            return rig
    except (OSError, ValueError):
        pass
    return copy.deepcopy(DEFAULT_RIG)


def validate_rig(rig, tag_ids, configured):
    """Raise ValueError on a rig that cannot be used; return warnings for tags Bermuda won't poll fast."""
    if rig.get("units") not in UNITS:
        raise ValueError(f"units must be one of {UNITS}")
    if not 0.0 < float(rig.get("snap_offset_m", 0)) <= 2.0:
        raise ValueError("snap offset must be between 0 and 2 m")
    if not 0.0 < float(rig.get("snap_radius_m", 0.6)) <= 3.0:
        raise ValueError("snap radius must be between 0 and 3 m")
    seen, warnings = set(), []
    for pole in rig.get("poles") or []:
        for s in pole.get("slots") or []:
            h = float(s.get("height_m"))
            if not 0.0 <= h <= MAX_HEIGHT:
                raise ValueError(f"pole {pole.get('id')} {s.get('slot')}: height {h} m outside 0-{MAX_HEIGHT} m")
            k = s.get("tag_key")
            if k is None:
                continue
            if k not in tag_ids:
                raise ValueError(f"pole {pole.get('id')} {s.get('slot')}: unknown tag {k!r}")
            if k in seen:
                raise ValueError(f"tag {tag_ids[k]} is on the rig twice")
            seen.add(k)
            if k not in configured:
                warnings.append({"tag_key": k, "warning": "not a configured Bermuda device"})
    return rig, warnings


def save_rig(calib_dir, rig, tag_ids, configured):
    rig, warnings = validate_rig(rig, tag_ids, configured)
    write_json_atomic(os.path.join(calib_dir, "rig.json"), rig)
    return rig, warnings


# ---- placements ---------------------------------------------------------------------------
def _floor_bounds(house, fi):
    f = house.floors[fi]
    res = getattr(house, "RES", 0.05)
    h, w = f["rf"].shape
    x0 = getattr(house, "X0", 0.0)                      # the frame origin (house.json), not always 0
    return x0, x0 + w * res, f["ymax"] - h * res, f["ymax"]


def resolve_placements(body, house, tag_ids):
    """Check a capture request and fill in tag id, room and z_abs. Returns (placements, warnings)."""
    rtype = body.get("type")
    if rtype not in ("pole", "loose"):
        raise ValueError("round type must be 'pole' or 'loose'")
    raw = body.get("placements") or []
    if not raw:
        raise ValueError("no placements")
    out, warnings, seen = [], [], set()
    for p in raw:
        k = p.get("tag_key")
        if k not in tag_ids:
            raise ValueError(f"unknown tag {k!r}")
        if k in seen:
            raise ValueError(f"tag {tag_ids[k]} is placed twice")
        seen.add(k)
        fi = int(p.get("floor", -1))
        if not 0 <= fi < len(house.floors):
            raise ValueError(f"no floor {fi}")
        x, y = float(p["x"]), float(p["y"])
        x0, x1, y0, y1 = _floor_bounds(house, fi)
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            raise ValueError(f"{tag_ids[k]}: ({x:.2f}, {y:.2f}) is outside the house")
        h = float(p["height_m"])
        if not 0.0 <= h <= MAX_HEIGHT:
            raise ValueError(f"{tag_ids[k]}: height {h} m outside 0-{MAX_HEIGHT} m")
        ctx = p.get("context") or "free"
        if ctx not in CONTEXTS:
            raise ValueError(f"{tag_ids[k]}: context must be one of {CONTEXTS}")
        pole, slot = p.get("pole"), p.get("slot")
        if rtype == "pole" and not (pole and slot):
            raise ValueError(f"{tag_ids[k]}: a pole round needs pole and slot for every tag")
        room = house.room_at(fi, x, y)
        if room is None:
            warnings.append(f"{tag_ids[k]} is outside every room")
        out.append(dict(tag_key=k, tag=tag_ids[k], pole=pole if rtype == "pole" else None,
                        slot=slot if rtype == "pole" else None, floor=fi, room=room,
                        floor_id=house.ids[fi] if getattr(house, "ids", None) else None,   # a floor added below
                                                                                          # moves the index
                        x=round(x, 4), y=round(y, 4), z_abs=round(house.floor_z(fi, room) + h, 4),
                        height_m=h, context=ctx, note=str(p.get("note") or ""), snapped=p.get("snapped")))
    return out, warnings


# ---- samples and quality ------------------------------------------------------------------
def summarise_samples(samples_by_key, tag_ids):
    """{key: {scanner: [[stamp, rssi]]}} -> (data {tag: {scanner: {n, med}}}, raw {tag: {scanner: [...]}})."""
    data, raw = {}, {}
    for k, per in samples_by_key.items():
        per = {s: v for s, v in per.items() if v}
        if not per:
            continue
        tag = tag_ids.get(k, k)
        data[tag] = {s: {"n": len(v), "med": float(st.median(r for _, r in v))} for s, v in per.items()}
        raw[tag] = per
    return data, raw


def gap_seconds(polls, t0, t1, allowed=GAP_ALLOWED_S):
    """Seconds of HA silence during [t0, t1]: every interval between good polls beyond the allowance."""
    ts = [t0] + sorted(t for t in polls if t0 <= t <= t1) + [t1]
    return float(sum(max(0.0, b - a - allowed) for a, b in zip(ts, ts[1:])))


def quality(tags, data, live_scanners, dead_scanners, gap_s, duration_s):
    dead = sorted(set(dead_scanners))
    gap_amber = gap_s > 0.2 * duration_s
    out = {}
    for tag in tags:
        per = data.get(tag) or {}
        heard = [s for s, v in per.items() if s in live_scanners and s not in dead and v["n"] >= 5]
        total = int(sum(v["n"] for v in per.values()))
        if total == 0 or len(heard) < 3:
            verdict = "red"
        elif len(heard) >= min(5, len(live_scanners)) and total >= 100:      # a small house: all its receivers
            verdict = "green"
        else:
            verdict = "amber"
        if verdict == "green" and (dead or gap_amber):
            verdict = "amber"
        out[tag] = {"verdict": verdict, "scanners": len(heard), "samples": total}
    return {"tags": out, "dead": dead, "ha_gaps_s": round(gap_s, 1), "ha_gap_amber": gap_amber,
            "receivers": len(live_scanners)}


# ---- capture job --------------------------------------------------------------------------
class Busy(Exception):
    """A capture is already running or waiting for review."""


class CaptureJob:
    """One capture at a time: idle -> countdown -> recording -> review -> (keep | redo) -> idle.

    Runs on the server so the phone can sleep. The recorder lives on the Bermuda poller (src);
    live tracking keeps using the same poll. `inline=True` runs the job in the caller's thread
    (tests); otherwise a daemon thread runs it.
    """

    def __init__(self, src, house, calib_dir, tag_ids_fn, live_fn, model_info=None, clock=time.time,
                 sleep=time.sleep, min_s=None, inline=False, on_keep=None, rig_fn=None, log=print):
        self.src, self.house, self.calib_dir = src, house, calib_dir
        self.tag_ids_fn, self.live_fn, self.model_info = tag_ids_fn, live_fn, model_info or {}
        self.clock, self.sleep, self.inline, self.on_keep, self.log = clock, sleep, inline, on_keep, log
        self.rig_fn = rig_fn or (lambda: load_rig(calib_dir))
        self.min_s = float(os.environ.get("RTLS_CALIB_MIN_S", "60")) if min_s is None else float(min_s)
        self.lock = threading.Lock()
        self.state, self.job, self.last_outcome, self.outcomes = "idle", None, None, {}
        self._n = 0

    @property
    def locked(self):
        return self.state != "idle"

    # -- control ---------------------------------------------------------------------------
    def start(self, body):
        countdown = float(body.get("countdown_s", 30))
        duration = float(body.get("duration_s", 180))
        if not 0 <= countdown <= 120:
            raise ValueError("countdown must be 0-120 s")
        if not self.min_s <= duration <= 600:
            raise ValueError(f"duration must be {self.min_s:g}-600 s")
        co_location = bool(body.get("co_location"))
        if co_location and body.get("type") != "loose":
            raise ValueError("co-location is a loose round")
        placements, warnings = resolve_placements(body, self.house, self.tag_ids_fn())
        with self.lock:
            if self.state != "idle":
                raise Busy(f"a capture is {self.state}")
            self._n += 1
            now = self.clock()
            j = dict(id=f"j{int(now * 1000)}-{self._n}", type=body["type"],
                     flags={"validation": bool(body.get("validation")), "co_location": co_location},
                     placements=placements, warnings=warnings, keys=[p["tag_key"] for p in placements],
                     countdown_s=countdown, duration_s=duration, t_start=now, t_rec=None,
                     cancel=False, error=None, quality=None, rig=self.rig_fn())
            self.job, self.state = j, "countdown"
        if self.inline:
            self._run(j)
        else:
            threading.Thread(target=self._run, args=(j,), daemon=True).start()
        return self.status()

    def cancel(self, outcome="cancelled"):
        with self.lock:
            j = self.job
            if j is None:
                return self.status_unlocked()
            if self.state in ("countdown", "recording"):
                j["cancel"], j["outcome_on_cancel"] = True, outcome   # the runner stops and finishes
                return self.status_unlocked()
        self._finish(j, outcome)
        return self.status()

    def redo(self):
        return self.cancel(outcome="discarded")

    def keep(self):
        with self.lock:
            j = self.job
            if self.state != "review" or j is None or j.get("error"):
                raise ValueError("nothing to keep")
            rid = new_id("r", j["t_rec"])
            rnd = dict(id=rid, type=j["type"], flags=j["flags"], t0=j["t_rec"], countdown_s=j["countdown_s"],
                       duration_s=j["duration_s"], rig=j["rig"], model=self.model_info,
                       dead_scanners=j["dead"], ha_gaps_s=j["quality"]["ha_gaps_s"], warnings=j["warnings"],
                       placements=j["placements"], data=j["data"], raw=j["raw"], quality=j["quality"]["tags"])
            path = os.path.join(self.calib_dir, "rounds", rid + ".json")
            write_json_atomic(path, rnd, overwrite=False)
            self._record_outcome(j, "kept", round_id=rid)
            self.job, self.state = None, "idle"
        if self.on_keep:
            self.on_keep(rid)
        return {"id": rid, "path": path}

    # -- runner ----------------------------------------------------------------------------
    def _run(self, j):
        recording = False
        try:
            end = j["t_start"] + j["countdown_s"]
            while self.clock() < end and not j["cancel"]:
                self.sleep(min(0.5, end - self.clock()))
            if j["cancel"]:
                return self._finish(j, j.get("outcome_on_cancel", "cancelled"))
            with self.lock:
                self.state = "recording"
            self.src.start_recording(j["keys"])
            recording = True
            j["t_rec"] = self.clock()
            dead = set(self.src.dead())
            end = j["t_rec"] + j["duration_s"]
            while self.clock() < end and not j["cancel"]:
                self.sleep(min(1.0, end - self.clock()))
                dead |= set(self.src.dead())
            rec = self.src.stop_recording()
            recording = False
            if j["cancel"]:
                return self._finish(j, j.get("outcome_on_cancel", "cancelled"))
            data, raw = summarise_samples(rec["samples"], self.tag_ids_fn())
            gap = gap_seconds(rec["polls"], rec["t0"], rec["t1"]) if rec["polls"] else 0.0
            q = quality([p["tag"] for p in j["placements"]], data, self.live_fn(), dead, gap, j["duration_s"])
            with self.lock:
                j.update(data=data, raw=raw, quality=q, dead=sorted(dead))
                self.state = "review"
        except Exception as e:                         # never leave the job hanging
            if recording:
                try:
                    self.src.stop_recording()
                except Exception:
                    pass
            self.log("calibration capture failed:", type(e).__name__, e)
            with self.lock:
                j["error"] = f"{type(e).__name__}: {e}"[:300]
                self.state = "review"

    def _record_outcome(self, j, outcome, **extra):
        self.last_outcome = dict(job_id=j["id"], outcome=outcome, **extra)
        self.outcomes[j["id"]] = self.last_outcome
        if len(self.outcomes) > 50:
            self.outcomes.pop(next(iter(self.outcomes)))

    def _finish(self, j, outcome):
        with self.lock:
            self._record_outcome(j, outcome)
            if self.job is j:
                self.job, self.state = None, "idle"

    # -- status ----------------------------------------------------------------------------
    def status(self, job_id=None):
        with self.lock:
            return self.status_unlocked(job_id)

    def status_unlocked(self, job_id=None):
        j = self.job
        st = dict(state=self.state, locked=self.state != "idle", job_id=j["id"] if j else None,
                  last_outcome=self.last_outcome)
        if job_id is not None:
            st["interrupted"] = (j is None or j["id"] != job_id) and job_id not in self.outcomes
        if j is not None:
            now = self.clock()
            if self.state == "countdown":
                st["seconds_left"] = max(0.0, round(j["t_start"] + j["countdown_s"] - now, 1))
            elif self.state == "recording" and j["t_rec"] is not None:
                st["seconds_left"] = max(0.0, round(j["t_rec"] + j["duration_s"] - now, 1))
                tag_of = {p["tag_key"]: p["tag"] for p in j["placements"]}
                st["counts"] = {tag_of.get(k, k): n for k, n in self.src.recording_counts().items()}
            st.update(type=j["type"], flags=j["flags"], warnings=j["warnings"], countdown_s=j["countdown_s"],
                      duration_s=j["duration_s"],
                      placements=[{k: p[k] for k in ("tag", "pole", "slot", "floor", "x", "y", "height_m", "room")}
                                  for p in j["placements"]])
            if self.state == "review":
                st.update(quality=j["quality"], error=j["error"])
        return st


# ---- convergence queue --------------------------------------------------------------------
def subprocess_runner(root, calib_dir=None, script=None, timeout_s=1800, python=None, use_nice=True):
    """runner(round_id, out_path) -> check dict: solver/convergence.py as a low-priority process."""
    import shutil
    import subprocess
    import sys
    script = script or os.path.join(root, "solver", "convergence.py")
    python = python or sys.executable
    nice = ["nice", "-n", "10"] if use_nice and shutil.which("nice") else []

    def run(round_id, out_path):
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        cmd = nice + [python, script, "--calib-dir", calib_dir or os.path.dirname(os.path.dirname(out_path)),
                      "--out", out_path, "--newest", round_id]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, cwd=os.path.dirname(script))
        except subprocess.TimeoutExpired:
            out = dict(round=round_id, status="failed", error=f"timed out after {timeout_s:g} s", t=time.time())
            write_json_atomic(out_path, out)
            return out
        try:
            return json.load(open(out_path, encoding="utf-8"))
        except (OSError, ValueError):
            out = dict(round=round_id, status="failed", t=time.time(),
                       error=f"exit {p.returncode}: {(p.stderr or p.stdout or '')[-500:]}")
            write_json_atomic(out_path, out)
            return out
    return run


class ConvergenceQueue:
    """Every kept round queues one check; every queued check runs, oldest first. The UI shows the
    latest finished result and how many are waiting. Survives restarts via recover()."""

    def __init__(self, calib_dir, runner, autostart=True, log=print):
        from collections import deque
        self.calib_dir, self.runner, self.log = calib_dir, runner, log
        self.checks_dir = os.path.join(calib_dir, "checks")
        self.q, self.running, self.latest = deque(), None, None
        self.cv = threading.Condition()
        if autostart:
            threading.Thread(target=self._worker, daemon=True).start()

    def enqueue(self, round_id):
        with self.cv:
            self.q.append(round_id)
            self.cv.notify()

    def recover(self):
        """Queue every kept round that has no check yet, and load the newest finished check."""
        done, newest = set(), None
        for fn in sorted(os.listdir(self.checks_dir)) if os.path.isdir(self.checks_dir) else []:
            try:
                c = json.load(open(os.path.join(self.checks_dir, fn), encoding="utf-8"))
            except (OSError, ValueError):
                continue
            done.add(c.get("round"))
            if newest is None or (c.get("t") or 0) >= (newest.get("t") or 0):
                newest = c
        rdir = os.path.join(self.calib_dir, "rounds")
        pending = sorted(f[:-5] for f in os.listdir(rdir) if f.endswith(".json")) if os.path.isdir(rdir) else []
        with self.cv:
            self.latest = newest
            for rid in pending:
                if rid not in done and rid not in self.q:
                    self.q.append(rid)
            self.cv.notify()

    def status(self):
        with self.cv:
            latest = {k: v for k, v in (self.latest or {}).items() if k != "trace"} or None
            return dict(queued=len(self.q), running=self.running, latest=latest)

    def _one(self, round_id):
        out_path = os.path.join(self.checks_dir, new_id("c", time.time()) + f"_{round_id}.json")
        try:
            out = self.runner(round_id, out_path)
        except Exception as e:
            out = dict(round=round_id, status="failed", error=f"{type(e).__name__}: {e}"[:500], t=time.time())
            write_json_atomic(out_path, out)
        self.log(f"convergence check for {round_id}: {out.get('status')}")
        return out

    def run_pending(self):
        """Process the whole queue in the caller's thread (tests)."""
        while True:
            with self.cv:
                if not self.q:
                    return
                rid = self.q.popleft()
                self.running = rid
            out = self._one(rid)
            with self.cv:
                self.running, self.latest = None, out

    def _worker(self):
        while True:
            with self.cv:
                while not self.q:
                    self.cv.wait()
                rid = self.q.popleft()
                self.running = rid
            out = self._one(rid)
            with self.cv:
                self.running, self.latest = None, out


# ---- coverage (information only; the stopping rule is convergence) -------------------------
def load_kept_rounds(calib_dir):
    rdir = os.path.join(calib_dir, "rounds")
    out = []
    for fn in sorted(os.listdir(rdir)) if os.path.isdir(rdir) else []:
        if fn.endswith(".json"):
            try:
                out.append(json.load(open(os.path.join(rdir, fn), encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return out


def _band(d):
    return "<3m" if d < 3.0 else ("3-8m" if d <= 8.0 else ">8m")


def coverage(rounds, scanners, flagged=()):
    """rounds: kept round dicts; scanners: {name: {x, y, z_abs, floor}}; flagged: {(round, tag)}."""
    import math
    rooms, placements = {}, []
    sc = {s: {b: {"same": 0, "other": 0} for b in ("<3m", "3-8m", ">8m")} for s in scanners}
    for r in rounds:
        flags = r.get("flags") or {}
        val = bool(flags.get("validation"))
        pole_counted = set()
        for p in r.get("placements") or []:
            key = (p["floor"], p.get("room"))
            row = rooms.setdefault(key, dict(floor=p["floor"], room=p.get("room"), pole_rounds=0,
                                             loose_points=0, validation_points=0))
            if val:
                row["validation_points"] += 1
            elif r.get("type") == "pole":
                if key not in pole_counted:
                    row["pole_rounds"] += 1
                    pole_counted.add(key)
            else:
                row["loose_points"] += 1
            placements.append(dict(round=r["id"], tag=p["tag"], floor=p["floor"], x=p["x"], y=p["y"],
                                   kind=r.get("type"), validation=val, flagged=(r["id"], p["tag"]) in set(flagged)))
            for s, v in ((r.get("data") or {}).get(p["tag"]) or {}).items():
                if s not in scanners or v.get("n", 0) < 5:
                    continue
                S = scanners[s]
                d = math.dist((p["x"], p["y"], p["z_abs"]), (S["x"], S["y"], S["z_abs"]))
                sc[s][_band(d)]["same" if S["floor"] == p["floor"] else "other"] += 1
    return dict(rooms=sorted(rooms.values(), key=lambda r: (r["floor"], str(r["room"]))), scanners=sc,
                placements=placements)
