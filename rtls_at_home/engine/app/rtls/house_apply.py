"""Applying a draft house (spec docs/specs/2026-09-30-map-builder-design.md, section 5): the history of applied
houses, the apply job, and the guard that checks the engine's first start on a new house.

Everything lives in the data folder (RTLS_SESSIONS): `house.json` (the live house, once one is applied),
`history/house_<UTC>.json` (the houses it replaced, the last 20), `apply.json` (the last apply's status, which
outlives the restart), `apply/pending.json` (the draft as it was when Apply was pressed)."""
import calendar
import json
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "solver"))
import house_check as HC      # noqa: E402
import house_doc as HD        # noqa: E402
import house_receivers as HR  # noqa: E402

HISTORY_KEEP = 20
VERSION = re.compile(r"house_(\d{8}T\d{6}Z)(_\d+)?\.json")


def _history_dir(data_dir):
    return os.path.join(data_dir, "history")


# ---- the history ------------------------------------------------------------------------------------------------
def snapshot(data_dir, doc, now=None):
    """Keeps a copy of `doc` as history/house_<UTC>.json (a second copy in the same second gets _2, _3, ...); returns
    its name."""
    d = _history_dir(data_dir)
    os.makedirs(d, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if now is None else now))
    name, n = f"house_{stamp}.json", 1
    while os.path.exists(os.path.join(d, name)):
        n += 1
        name = f"house_{stamp}_{n}.json"
    HD.save(doc, os.path.join(d, name))
    return name


def _versions(data_dir):
    d = _history_dir(data_dir)
    names = [n for n in os.listdir(d) if VERSION.fullmatch(n)] if os.path.isdir(d) else []
    key = lambda n: (VERSION.fullmatch(n).group(1), int((VERSION.fullmatch(n).group(2) or "_1")[1:]))
    return sorted(names, key=key, reverse=True)                     # newest first


def history(data_dir):
    """The kept versions, newest first: [{name, at (epoch s), floors, rooms, hash}]; unreadable ones are left out."""
    out = []
    for name in _versions(data_dir):
        try:
            with open(os.path.join(_history_dir(data_dir), name), encoding="utf-8") as f:
                doc = json.load(f)
            at = calendar.timegm(time.strptime(VERSION.fullmatch(name).group(1), "%Y%m%dT%H%M%SZ"))
            out.append(dict(name=name, at=at, floors=len(doc["floors"]),
                            rooms=sum(len(fl.get("rooms") or []) for fl in doc["floors"]), hash=HD.doc_hash(doc)[:12]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def prune_history(data_dir, keep=HISTORY_KEEP):
    for name in _versions(data_dir)[keep:]:
        try:
            os.remove(os.path.join(_history_dir(data_dir), name))
        except OSError:
            pass


def history_doc(data_dir, name):
    """A kept version by name; ValueError for anything that is not one (names come from the browser)."""
    if not isinstance(name, str) or not VERSION.fullmatch(name):
        raise ValueError("no such version")
    p = os.path.join(_history_dir(data_dir), name)
    if not os.path.isfile(p):
        raise ValueError("no such version")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# ---- the apply job ----------------------------------------------------------------------------------------------
STEPS = [("checking", "Check the draft"), ("build", "Build the maps"),
         ("geometry", "Start the engine on the new house (geometry and fit)"),
         ("history", "Keep the current house in the history"), ("switching", "Switch to the new house"),
         ("restarting", "Restart the engine")]
RESTART = 3                   # the exit code that asks the supervisor (Swarm's on-failure policy) for a restart


class Busy(Exception):
    """An apply is already running."""


class ProblemsError(ValueError):
    def __init__(self, problems):
        super().__init__(f"{len(problems)} problems to fix first")
        self.problems = problems


def write_status(data_dir, status):
    HD.save(status, os.path.join(data_dir, "apply.json"))


def read_status(data_dir):
    try:
        with open(os.path.join(data_dir, "apply.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def references(eng):
    """The rooms the engine's receivers (where they stand and stood) and a fixed survey's anchors name, by floor id and
    room name: {(floor id, room name): [names]}. A draft that renames or removes one cannot start the engine.
    Calibration rounds are not references: a renamed room's round takes the room it stands in."""
    refs = {}
    for S in (getattr(eng.D, "S", None) or {}, getattr(eng.D, "S_now", None) or {}):
        for name, v in S.items():
            if v.get("room") and v.get("floor"):
                refs.setdefault((v["floor"], v["room"]), []).append(name)
    for e in getattr(eng.D, "E", None) or []:
        if e.get("round"):                  # calibration rounds follow a rename (calib_loader._room_of)
            continue
        if e.get("room") and e.get("fi") is not None and 0 <= e["fi"] < len(eng.house.ids):
            refs.setdefault((eng.house.ids[e["fi"]], e["room"]), []).append(str(e.get("id") or e.get("group")))
    return {k: sorted(set(v)) for k, v in refs.items()}


def house_ready(doc, registry=None):
    """Whether the engine can run on a house: (True, None), or (False, what it lacks first - "rooms" or "receivers").
    It needs a room on some floor and a placed receiver: the document's, or for a house without any the registry its
    installation names (RP_REGISTRY, house_receivers.registry_file)."""
    if not any(f.get("rooms") for f in doc.get("floors") or []):
        return False, "rooms"
    rx = HR.legacy(doc) if doc.get("receivers") else (registry or HR.registry_file)()
    if not any(v.get("x") is not None for v in rx.values()):
        return False, "receivers"
    return True, None


def _errors(doc, refs=None):
    try:
        probs = HC.check(doc, refs=refs)
    except Exception as e:                  # a document the checks cannot even read is one problem
        return [dict(code="doc", severity="error", message=f"cannot check this document ({type(e).__name__}: {e})")]
    return [p for p in probs if p["severity"] == "error"]


def dry_run(pending, progress, timeout_s=1200, cmd=None):
    """Builds the pending house and starts a whole Engine on it in a child process (the live engine keeps running and
    the geometry cache is filled for the restart); progress(step) for each step; returns the child's last report
    ({"ok": true, "engine": false} when the house has no receiver yet and no engine was started); RuntimeError with
    the child's last words when it fails, or when it has not finished in timeout_s (it is stopped)."""
    env = dict(os.environ, RTLS_HOUSE=os.path.abspath(pending), PYTHONUNBUFFERED="1")
    cmd = cmd or [sys.executable, os.path.abspath(__file__), "--dry-run", os.path.abspath(pending)]
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace")
    timed_out = threading.Event()

    def stop():
        timed_out.set()
        p.kill()

    watchdog = threading.Timer(timeout_s, stop)
    watchdog.daemon = True
    watchdog.start()
    last, report = "", {}
    for line in p.stdout:
        line = line.strip()
        msg = None
        if line.startswith("{"):
            try:
                msg = json.loads(line)
            except ValueError:
                msg = None
        if isinstance(msg, dict) and "step" in msg:
            progress(msg["step"])
        elif isinstance(msg, dict) and "error" in msg:
            last = str(msg["error"])
        elif isinstance(msg, dict) and msg.get("ok"):
            report = msg
        elif line:
            last = line
    code = p.wait()
    watchdog.cancel()
    if timed_out.is_set():
        raise RuntimeError(f"the dry run did not finish in {timeout_s // 60} min; it was stopped")
    if code != 0:
        raise RuntimeError(last or f"the dry run stopped with code {code}")
    return report


class Applier:
    """One apply at a time (spec section 5): check, build, start an engine on the new house, keep the current house in
    the history, switch, restart. Any failure before the switch leaves the old house live and the draft untouched."""

    def __init__(self, data_dir, store, run_dry=dry_run, exit_fn=os._exit, before_exit=None, log=print, delay=1.5):
        self.dir, self.store, self.run_dry, self.exit_fn = data_dir, store, run_dry, exit_fn
        self.before_exit, self.log, self.delay = before_exit, log, delay
        self.lock = threading.Lock()
        self.thread = None
        self.st = None

    def _status(self, state, **kw):
        """Records state (a step id, or failed) with the steps' own states, in memory and in apply.json."""
        order = [k for k, _ in STEPS]
        st = dict(self.st or {}, state=state, **kw)
        failed = state == "failed"
        cur = st.get("step") if failed else state
        ci = order.index(cur) if cur in order else None
        st["steps"] = [dict(id=k, label=lbl,
                            state="failed" if failed and i == ci else "done" if ci is not None and i < ci
                            else "running" if not failed and i == ci else "waiting")
                       for i, (k, lbl) in enumerate(STEPS)]
        _skipped(st)
        if not failed:
            st["step"] = state
        self.st = st
        write_status(self.dir, st)

    def status(self):
        with self.lock:
            if self.st is not None:
                return json.loads(json.dumps(self.st))
        return read_status(self.dir) or dict(state="idle", steps=[])

    def start(self):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                raise Busy("an apply is already running")
            if not self.store.has_draft():
                raise ValueError("nothing to apply: there is no draft")
            doc = self.store.draft()
            errs = _errors(doc, getattr(self.store, "refs", None))
            if errs:
                raise ProblemsError(errs)
            pending = os.path.join(self.dir, "apply", "pending.json")
            HD.save(doc, pending)                             # what goes live is the draft as it is now
            self.st = dict(hash=HD.doc_hash(doc)[:12], started=time.time(), finished=None, error=None, previous=None)
            self._status("build")
            self.thread = threading.Thread(target=self._run, args=(doc, pending), daemon=True, name="apply")
            self.thread.start()
        return self.status()

    def _run(self, doc, pending):
        try:
            report = self.run_dry(pending, lambda step: self._status(step) if step in ("build", "geometry") else None)
            if isinstance(report, dict) and report.get("engine") is False:   # no receiver yet: maps only (setup)
                self.st["engine"] = False
            self._status("history")
            previous = snapshot(self.dir, HD.load())     # a new household's first: the starting house
            prune_history(self.dir)
            self._status("switching", previous=previous)
            os.replace(pending, os.path.join(self.dir, "house.json"))
            self.store.discard_draft_if(HD.doc_hash(doc))    # a draft edited meanwhile is newer: it stays
            self._status("restarting", finished=time.time())
        except Exception as e:
            self.log("apply failed:", type(e).__name__, e)
            try:
                os.remove(pending)
            except OSError:
                pass
            self._status("failed", error=str(e), finished=time.time())
            return
        self.log("applied house", self.st["hash"], "- restarting")
        try:
            if self.before_exit:
                self.before_exit()
        finally:
            time.sleep(self.delay)
            self.exit_fn(RESTART)

    def restore(self, name):
        """A history version into the draft (it replaces the draft); the editor's opening state after it."""
        self.store.save_draft(history_doc(self.dir, name))
        return self.store.state()

    def join(self, timeout=None):
        if self.thread is not None:
            self.thread.join(timeout)


# ---- the first start after an apply -----------------------------------------------------------------------------
def _final(st, state, at="restarting", **kw):
    """The status with every step done, or failed at step `at` (the steps before it done, after it waiting)."""
    order = [k for k, _ in STEPS]
    ai = order.index(at) if at in order else len(order) - 1
    steps = [dict(id=k, label=lbl, state="done" if state != "failed" or i < ai else "failed" if i == ai else "waiting")
             for i, (k, lbl) in enumerate(STEPS)]
    return _skipped(dict(st, state=state, steps=steps, **kw))


def _skipped(st):
    """The engine step of an apply whose dry run started no engine (no receiver yet) is skipped, not done."""
    if st.get("engine") is False:
        for s in st.get("steps") or []:
            if s["id"] == "geometry" and s["state"] == "done":
                s["state"] = "skipped"
    return st


def _remove(p):
    try:
        os.remove(p)
    except OSError:
        pass


def _roll_back(data_dir, st, log):
    """Puts the previous house back after the engine failed to start on the new one, keeping the new one: as the
    draft again, or (when there is a newer draft) in apply/rejected.json. Returns what it did, for the editor."""
    hp, draft = os.path.join(data_dir, "house.json"), os.path.join(data_dir, "house.draft.json")
    kept = ""
    try:
        with open(hp, encoding="utf-8") as f:
            rejected = json.load(f)
        if not os.path.exists(draft):
            HD.save(rejected, draft)
            kept = "the new house is your draft again"
        else:
            HD.save(rejected, os.path.join(data_dir, "apply", "rejected.json"))
            kept = "your newer draft is unchanged; the new house is kept in apply/rejected.json"
    except (OSError, ValueError) as e:
        log("could not keep the rejected house:", e)
    try:
        if not st.get("previous"):
            raise ValueError("no previous house recorded")
        HD.save(history_doc(data_dir, st["previous"]), hp)
        back = "the previous house is back"
    except (OSError, ValueError) as e:      # no rollback target: the engine falls back to the starting house
        log("could not put the previous house back:", e, "- falling back to the starting house")
        _remove(hp)
        back = "the starting house is back"
    return f"{back}; {kept}" if kept else back


def boot(data_dir, start_engine, exit_fn=os._exit, log=print):
    """Starts the engine, minding an apply that a restart interrupted (spec section 5):
    - died before the switch (checking ... history): the apply is marked failed and nothing changes;
    - after the switch (switching, restarting): a start that fails puts the previous house back (keeping the new one
      as the draft), records why and asks for another restart; one that works marks the apply done and prunes."""
    st = read_status(data_dir)
    state = (st or {}).get("state")
    if state in ("checking", "build", "geometry", "history"):
        _remove(os.path.join(data_dir, "apply", "pending.json"))
        write_status(data_dir, _final(st, "failed", at=state, finished=time.time(),
                                      error="the engine restarted during the apply; nothing was switched"))
        return start_engine()
    if state not in ("switching", "restarting"):
        return start_engine()
    t0 = time.time()
    try:
        eng = start_engine()
    except Exception as e:
        log("the engine did not start on the new house:", type(e).__name__, e, "- putting the previous house back")
        what = _roll_back(data_dir, st, log)
        write_status(data_dir, _final(st, "failed", finished=time.time(),
                                      error=f"the engine did not start on the new house ({type(e).__name__}: {e}); {what}"))
        exit_fn(RESTART)
        raise
    write_status(data_dir, _final(st, "done", booted=time.time()))
    try:
        prune(data_dir, since=t0)
    except Exception as e:                  # tidying up must never stop the engine
        log("prune:", type(e).__name__, e)
    return eng


def _referenced_underlays(data_dir):
    docs = []
    for p in (os.path.join(data_dir, "house.json"), os.path.join(data_dir, "house.draft.json")):
        try:
            with open(p, encoding="utf-8") as f:
                docs.append(json.load(f))
        except (OSError, ValueError):
            pass
    for name in _versions(data_dir):
        try:
            docs.append(history_doc(data_dir, name))
        except (OSError, ValueError):
            pass
    return {os.path.basename(fl["underlay"]["file"]) for d in docs for fl in d.get("floors") or []
            if isinstance(fl.get("underlay"), dict) and fl["underlay"].get("file")}


def prune(data_dir, since=None, keep_caches=4):
    """Tidies the data folder after a good start: builds but the current house's and the previous one's (the newest
    history version: a restore is quick), feature caches but the ones this start read or wrote (mtime since `since`)
    and the newest few others, and images that no house, draft or history version uses."""
    import shutil
    import house_build as HB
    bd = os.path.join(data_dir, "build")
    if os.path.isdir(bd):
        keep = set()
        docs = [lambda: HD.load(HD.default_path())]
        vs = _versions(data_dir)
        if vs:
            docs.append(lambda: history_doc(data_dir, vs[0]))
        for get in docs:
            try:
                keep.add(HB.build_name(get()))
            except Exception:
                pass
        for n in os.listdir(bd):
            if n not in keep:
                shutil.rmtree(os.path.join(bd, n), ignore_errors=True)
    cd = os.path.join(data_dir, "cache")
    if os.path.isdir(cd):
        banks = sorted((n for n in os.listdir(cd) if n.startswith(".bank_")),
                       key=lambda n: os.path.getmtime(os.path.join(cd, n)), reverse=True)
        used = [n for n in banks if since is not None and os.path.getmtime(os.path.join(cd, n)) >= since - 1]
        rest = [n for n in banks if n not in used]
        for n in rest[keep_caches:]:
            try:
                os.remove(os.path.join(cd, n))
            except OSError:
                pass
    ud = os.path.join(data_dir, "underlays")
    if os.path.isdir(ud):
        used = _referenced_underlays(data_dir)
        for n in os.listdir(ud):
            if n not in used:
                try:
                    os.remove(os.path.join(ud, n))
                except OSError:
                    pass


# ---- the dry run (a child process) ------------------------------------------------------------------------------
def _dry_run_main(path):
    t0 = time.time()
    try:
        doc = HD.load(path)
        import house_build as HB
        print(json.dumps({"step": "build"}), flush=True)
        HB.ensure_build(doc)
        print(json.dumps({"step": "geometry"}), flush=True)
        if not house_ready(doc)[0]:          # no engine runs on it (setup mode): its maps are all it needs
            print(json.dumps({"ok": True, "s": round(time.time() - t0, 1), "engine": False}), flush=True)
            return
        import engine as E
        import house_api as HAPI
        eng = E.Engine(log=lambda *a: None)
        HAPI.house_payload(eng.house, eng.floor_names)      # what the server builds for the viewer at start-up
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), flush=True)
        sys.exit(1)
    print(json.dumps({"ok": True, "s": round(time.time() - t0, 1)}), flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--dry-run":
        _dry_run_main(sys.argv[2])
    else:
        sys.exit("usage: house_apply.py --dry-run <house.json>")
