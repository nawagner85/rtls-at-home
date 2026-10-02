"""The session log: what the live loop records for the replay evaluator and later analysis.

Nick 2026-09-30 (the engine will run on a Raspberry Pi; "we can probably significantly reduce the logging window"):
one unrotated file grew ~394 MB a day (raw readings 62%, est records 36%, each est carrying a full config copy) and
the folder kept everything since 09-22. Now:
  - one file an hour (RTLS_SESSION_ROTATE_S, 3600), each opening with its own `session` header (the current config,
    part number, the file it continues), so any hour reads on its own;
  - a closed file is gzipped off the loop thread (~8x), keeping its modification time;
  - session logs (rtls_<UTC>.jsonl[.gz]) older than RTLS_SESSION_KEEP_H (72) are deleted at start and on rotation.
    Nothing else in the folder is touched: devices, calibration rounds, level events, outlets live there too;
  - est records every RTLS_EST_EVERY_S (30) or when what they record changes (EstThrottle).
"""
import gzip
import json
import os
import re
import shutil
import threading
import time

ROTATE_S = float(os.environ.get("RTLS_SESSION_ROTATE_S", "3600"))
KEEP_H = float(os.environ.get("RTLS_SESSION_KEEP_H", "72"))
EST_EVERY_S = float(os.environ.get("RTLS_EST_EVERY_S", "30"))
LOG_NAME = re.compile(r"rtls_\d{8}T\d{6}Z\.jsonl(\.gz)?$")
PART_NAME = re.compile(r"rtls_\d{8}T\d{6}Z\.jsonl\.gz\.part$")


def jsonable(o):
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [jsonable(v) for v in o]
    if hasattr(o, "item"):
        return o.item()
    if isinstance(o, float) and o != o:
        return None
    return o


def compress(path):
    """path -> path.gz with path's modification time (retention counts from the data, not the compression); the
    original is removed only once the .gz is complete."""
    tmp = path + ".gz.part"
    try:
        with open(path, "rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        st = os.stat(path)
        os.utime(tmp, (st.st_atime, st.st_mtime))
        os.replace(tmp, path + ".gz")
        os.remove(path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


class SessionLog:
    """write(type, payload) appends one JSON line to the current hour's file. header() gives the current header."""

    def __init__(self, folder, header, rotate_s=None, keep_h=None, clock=time.time, background=True):
        os.makedirs(folder, exist_ok=True)
        self.folder, self.header, self.clock, self.background = folder, header, clock, background
        self.rotate_s = ROTATE_S if rotate_s is None else float(rotate_s)
        self.keep_h = KEEP_H if keep_h is None else float(keep_h)
        self.lock = threading.Lock()
        self.jobs = []
        self.name = self.path = self.f = None
        self.part = 0
        for n in os.listdir(folder):                          # interrupted compressions: their source is still there
            if PART_NAME.match(n):
                try:
                    os.remove(os.path.join(folder, n))
                except OSError:
                    pass
        leftovers = [p for p in self._logs() if p.endswith(".jsonl")]   # a previous run's files
        with self.lock:
            self._open(None)
        self._later(leftovers)

    def _logs(self):
        return sorted(os.path.join(self.folder, n) for n in os.listdir(self.folder) if LOG_NAME.match(n))

    def _open(self, prev):
        now = self.clock()
        self.name = "rtls_" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + ".jsonl"
        self.path = os.path.join(self.folder, self.name)
        self.f = open(self.path, "a", encoding="utf-8")
        self.opened = now
        self.part += 1
        head = dict(self.header() or {})
        head["part"] = self.part
        if prev:
            head["continues"] = prev
        self._line("session", head, now)

    def _line(self, typ, payload, now):
        rec = {"type": typ, "t": round(now, 3), **jsonable(payload)}
        self.f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self.f.flush()

    def write(self, typ, payload):
        with self.lock:
            if self.f is None:              # closed (an apply restarts the process): the last tick is dropped
                return
            now = self.clock()
            if now - self.opened >= self.rotate_s:
                old, prev = self.path, self.name
                self.f.close()
                self._open(prev)
                self._later([old])
            self._line(typ, payload, now)

    def _later(self, paths):
        if not self.background:
            return self._tidy(paths)
        th = threading.Thread(target=self._tidy, args=(paths,), daemon=True, name="session-tidy")
        self.jobs = [j for j in self.jobs if j.is_alive()] + [th]
        th.start()

    def _tidy(self, paths):
        for p in paths:
            if p != self.path and os.path.exists(p):
                compress(p)
        self.prune()

    def prune(self):
        """Delete session logs last written more than keep_h ago (never the current file, nothing else)."""
        cutoff = self.clock() - self.keep_h * 3600.0
        for p in self._logs():
            if p == self.path:
                continue
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass

    def wait(self):
        """Block until the background compressions and pruning are done (tests, shutdown)."""
        for j in list(self.jobs):
            j.join()

    def close(self):
        with self.lock:
            if self.f:
                self.f.close()
                self.f = None


class EstThrottle:
    """When the live loop writes an est record: every `every_s`, and at once when what it records changes - the
    receivers left out (dead or switched off) or any tracked device's reported room - so the replay's dead lists and
    the live answers it scores against stay exact at the moments that matter."""

    def __init__(self, every_s=None):
        self.every_s = EST_EVERY_S if every_s is None else float(every_s)
        self.last_t = self.last_sig = None

    def due(self, now, sig):
        if self.last_t is None or sig != self.last_sig or now - self.last_t >= self.every_s:
            self.last_t, self.last_sig = now, sig
            return True
        return False
