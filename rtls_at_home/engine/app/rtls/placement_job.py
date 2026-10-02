"""The placement advisor's background job (spec docs/specs/2026-10-01-placement-advisor-design.md section 2).

One run at a time: solver/placement.py in a child process at lower priority (the live engine keeps tracking), its JSON
progress lines passed on, cancel kills it. The status and the last finished result live in <data>/placement.json, with
the house and outlet set the result was computed for, so a result the house or the survey has moved past is stale.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time

from calib import write_json_atomic

HERE = os.path.dirname(os.path.abspath(__file__))
PLACEMENT = os.path.join(HERE, "..", "..", "solver", "placement.py")
TIMEOUT_S = 2 * 3600            # minutes on a NUC, longer on a Pi
MAX_PROXIES = 30
MAX_CHECK = 60                  # spots in a checked selection (a layout has one per proxy)


class Busy(Exception):
    """A placement run is already going."""


class Cancelled(Exception):
    """The run was cancelled."""


def outlets_key(outlets):
    """What the advisor reads of the survey (where each outlet is, and whether it is deaf), hashed: a note or a
    "current: ..." status changes nothing it computed."""
    rows = sorted((str(o.get("id")), o.get("floor"), o.get("x"), o.get("y"), o.get("z_rel"),
                   "deaf" in str(o.get("status") or "").lower()) for o in outlets or [])
    return hashlib.sha1(json.dumps(rows).encode()).hexdigest()[:12]


def _request(req, last=None):
    """{proxies, fixed} (a suggestion) or {check: [spot keys]} (a selection of the last suggestion, spec section 4)
    checked, or ValueError."""
    if not isinstance(req, dict):
        raise ValueError("send {proxies, fixed} or {check}")
    if "check" in req:
        keys = req["check"]
        if not last:
            raise ValueError("there is no suggestion to check a selection of: run the advisor first")
        if not isinstance(keys, list) or not keys or len(keys) > MAX_CHECK or not all(isinstance(k, str) for k in keys):
            raise ValueError(f"check must list 1 to {MAX_CHECK} spots")
        return dict(check=list(keys))
    n, fixed = req.get("proxies"), req.get("fixed", [])
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_PROXIES:
        raise ValueError(f"proxies must be a whole number from 1 to {MAX_PROXIES}")
    if not isinstance(fixed, list) or not all(isinstance(s, str) for s in fixed):
        raise ValueError("fixed must be a list of receiver names")
    return dict(proxies=n, fixed=list(fixed))


def child(job_path, out_path, progress, cancelled, cmd=None, timeout_s=TIMEOUT_S):
    """Runs placement.py on job_path, writing out_path; progress(dict) per JSON progress line. Cancelled when
    `cancelled` is set (the child is killed), RuntimeError with the child's last words when it fails or overruns."""
    cmd = (cmd or [sys.executable, os.path.abspath(PLACEMENT)]) + ["--job", job_path, "--out", out_path]
    nice = (lambda: os.nice(10)) if hasattr(os, "nice") else None
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace", env=dict(os.environ, PYTHONUNBUFFERED="1"), preexec_fn=nice)
    done, timed_out = threading.Event(), threading.Event()

    def watch():
        t_end = time.time() + timeout_s
        while not done.is_set():
            if cancelled.wait(0.2) or time.time() > t_end:
                if not cancelled.is_set():
                    timed_out.set()
                if p.poll() is None:
                    p.kill()
                return

    threading.Thread(target=watch, daemon=True, name="placement-watch").start()
    last = ""
    try:
        for line in p.stdout:
            line = line.strip()
            msg = None
            if line.startswith("{"):
                try:
                    msg = json.loads(line)
                except ValueError:
                    msg = None
            if isinstance(msg, dict) and "step" in msg:
                progress(msg)
            elif isinstance(msg, dict) and "error" in msg:
                last = str(msg["error"])
            elif line and not (isinstance(msg, dict) and msg.get("done")):
                last = line
        code = p.wait()
    finally:
        done.set()
    if cancelled.is_set():
        raise Cancelled()
    if timed_out.is_set():
        raise RuntimeError(f"the advisor did not finish in {timeout_s // 60} min; it was stopped")
    if code != 0:
        raise RuntimeError(last or f"the advisor stopped with code {code}")


class PlacementJob:
    def __init__(self, data_dir, run_child=child, log=print):
        self.dir, self.run_child, self.log = data_dir, run_child, log
        self.path = os.path.join(data_dir, "placement.json")
        self.work = os.path.join(data_dir, "placement")
        self.lock = threading.Lock()
        self.thread, self.cancelled = None, threading.Event()
        self.st = self._read()
        if self.st.get("state") == "running":                 # this process did not start it: the engine restarted
            self._set(state="failed", error="the engine restarted during the run; run it again", finished=time.time())

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                st = json.load(f)
            if isinstance(st, dict):
                return st
        except (OSError, ValueError):
            pass
        return dict(state="idle", progress=None, error=None, started=None, finished=None, asked=None, last=None)

    def _set(self, **kw):
        self.st = dict(self.st, **kw)
        try:
            write_json_atomic(self.path, self.st)
        except OSError as e:
            self.log("placement: cannot save the status:", e)

    def status(self, house=None, outlets=None):
        """The run's state and progress, and the last finished result with the request, house and outlet set it was
        for; stale when the given (live) house or outlet key differs."""
        with self.lock:
            st = json.loads(json.dumps(self.st))
        last = st.pop("last", None) or {}
        st.update(result=last.get("result"), request=last.get("request"), house=last.get("house"),
                  outlets=last.get("outlets"), computed=last.get("finished"), check=last.get("check"))
        st["stale"] = bool(last) and ((house is not None and last.get("house") != house)
                                      or (outlets is not None and last.get("outlets") != outlets))
        return st

    def start(self, req, outlets, house_hash, outlets_hash):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise Busy("the advisor is already running")
            req = _request(req, self.st.get("last"))
            last = self.st.get("last") or {}
            if "check" in req and (last.get("house") != house_hash or last.get("outlets") != outlets_hash):
                raise ValueError("the house or the outlets changed since this suggestion was worked out: run the "
                                 "advisor again, then check")
            os.makedirs(self.work, exist_ok=True)
            job_path = os.path.join(self.work, "job.json")
            job = dict(req, outlets=outlets)
            if "check" in req:                                 # scored with the suggestion's fixed receivers
                job.update(fixed=self.st["last"]["request"]["fixed"], proxies=self.st["last"]["request"]["proxies"])
            write_json_atomic(job_path, job)
            self.cancelled = threading.Event()
            self._set(state="running", progress=None, error=None, started=time.time(), finished=None, asked=req)
            self.thread = threading.Thread(target=self._run, args=(job_path, req, house_hash, outlets_hash),
                                           daemon=True, name="placement")
            self.thread.start()
        return self.status(house_hash, outlets_hash)

    def _progress(self, p):
        with self.lock:
            self._set(progress=p)

    def _run(self, job_path, req, house_hash, outlets_hash):
        out = os.path.join(self.work, "result.json")
        try:
            if os.path.exists(out):
                os.remove(out)
            self.run_child(job_path, out, self._progress, self.cancelled)
            if self.cancelled.is_set():
                raise Cancelled()
            with open(out, encoding="utf-8") as f:
                result = json.load(f)
            if "check" in req:                                 # beside the suggestion it is a selection of
                last = dict(self.st["last"], check=result)
            else:                                              # a new suggestion: an old check goes with the old one
                last = dict(result=result, request=req, house=house_hash, outlets=outlets_hash, finished=time.time())
            with self.lock:
                self._set(state="done", finished=time.time(), last=last)
            self.log("placement: done", req)
        except Exception as e:
            cancelled = self.cancelled.is_set() or isinstance(e, Cancelled)
            if not cancelled:
                self.log("placement failed:", type(e).__name__, e)
            with self.lock:
                self._set(state="cancelled" if cancelled else "failed", error=None if cancelled else str(e),
                          finished=time.time())
        finally:
            for p in (job_path, out):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def cancel(self):
        self.cancelled.set()
        return self.status()

    def join(self, timeout=None):
        if self.thread is not None:
            self.thread.join(timeout)
