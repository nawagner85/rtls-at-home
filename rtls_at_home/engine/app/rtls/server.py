"""RTLS live viewer: pulls Bermuda data from Home Assistant, runs the frozen v3 models, serves a
3-D view of the house. Standard library only for serving; numpy/scipy/opencv for the models.

  python app/rtls/server.py                                # live (HA_URL + HA_TOKEN / HA_TOKEN_FILE)
  python app/rtls/server.py --replay survey/blind_4.json --speed 4

Every session is logged (raw samples, estimates, and "I'm here" ground-truth labels) as JSONL in
./sessions, so a walk-around can be replayed and re-scored later against any model.

LAN / tailnet only by design: this is a live map of where a person is inside the house.
"""
import argparse
import hmac
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine as E            # noqa: E402
import collector as C         # noqa: E402
import calib as K             # noqa: E402
import devices as DV          # noqa: E402
import presence as PR          # noqa: E402
import onboard as OB           # noqa: E402
import outlet_survey as OU     # noqa: E402
import calibration as CB       # noqa: E402
import receivers as RX         # noqa: E402
import house_api as HA         # noqa: E402
import house_apply as HAP      # noqa: E402
import sessionlog as SL         # noqa: E402
import placement_job as PJ      # noqa: E402
import house_doc as HD          # noqa: E402
import ha_meta as HM            # noqa: E402

ROOT = E.ROOT
STATIC = os.path.join(HERE, "static")
STATIC_PATH = re.compile(r"/static/((?:[A-Za-z0-9_\-]+/)*[A-Za-z0-9_.\-]+)")


def static_file(path):
    """The file a /static/... request names, in subfolders too (the vendored three.js), or None: folder names have no
    dots, so nothing climbs out of static/."""
    m = STATIC_PATH.fullmatch(path)
    if not m:
        return None
    fp = os.path.join(STATIC, *m.group(1).split("/"))
    return fp if os.path.isfile(fp) else None
MAX_INGEST = 1024 * 1024            # bytes: a bridge batch is a few KB; refuse anything absurd
MAX_HOUSE = 16 * 1024 * 1024        # bytes: a house document or an underlay image (the map editor)
# The things that move (Nick 2026-09-26): phones and pets get a short window half-life and no room filter (the raw
# posterior plus the motion filter follows a person at 2-3 m/s); tags keep the configured half-life and the state
# machine. RTLS_HALFLIFE_MOBILE overrides (flags.py MOBILE_HALFLIFE).
MOBILE_HALFLIFE = float(os.environ.get("RTLS_HALFLIFE_MOBILE", "8"))
MOBILE_KINDS = ("phone", "pet")


def device_halflife(kind, base):
    return min(float(base), MOBILE_HALFLIFE) if kind in MOBILE_KINDS else float(base)


def device_filter(kind, cfg_filter):
    return bool(cfg_filter) and kind not in MOBILE_KINDS
LANDMARK_RANGE = 1.0                # m: "near the <fixture>" only within this distance


def dist_to_polygon(x, y, pts):
    """Metres from (x, y) to a polygon: 0 inside, else to the nearest edge."""
    inside, n = False, len(pts)
    for i in range(n):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    if inside:
        return 0.0
    best = float("inf")
    for i in range(n):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
        best = min(best, float(np.hypot(x - (x1 + t * dx), y - (y1 + t * dy))))
    return best


def landmarks_from(fixtures, room_of=None):
    """(floor, name, polygon, room) for every fixture worth naming: furniture, not walls or stair parts. Names come
    from fixture ids until the map editor gives fixtures real names (spec section 11). room_of(floor, x, y) gives the
    room the fixture's centre is in, so a landmark is only used for devices in the same room."""
    out = []
    for f in fixtures:
        fid = f.get("id") or ""
        if f.get("rf") in ("low", "med", "high") and "wall" not in fid and not fid.startswith("stair"):
            pts = [tuple(p) for p in f["points"]]
            cx, cy = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
            room = room_of(f["floor"], cx, cy) if room_of else None
            out.append((f["floor"], fid.replace("_", " "), pts, room))
    return out


def nearest_landmark(landmarks, floor, x, y, within=LANDMARK_RANGE, room=None):
    """Closest landmark within `within` metres on the same floor, and in `room` when both are known."""
    best = None
    for fi, name, pts, lroom in landmarks:
        if fi == floor and (room is None or lroom is None or lroom == room):
            d = dist_to_polygon(x, y, pts)
            if d <= within and (best is None or d < best[0]):
                best = (d, name)
    return best[1] if best else None


def describe(room, near):
    """The plain-English location a voice assistant reads out."""
    if not room:
        return None
    return f"near the {near} in the {room}" if near else f"in the {room}"
TICK = 2.0


# The devices an installation starts with (devices.json's first contents): solver/devices_seed.json where the
# installation has one (the developer's: a phone, tags and WAP self-tests), none otherwise - a new household adds its
# devices in Setup -> Onboard. After the first start devices.json is the source of truth.
SEED_PATH = os.environ.get("RTLS_SEED", os.path.join(E.SOLVER, "devices_seed.json"))


def load_seed(path):
    """{key: {name, kind, calib_id?, default?}} from a seed file, or {} without one."""
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f).get("devices") or []
    except (OSError, ValueError, AttributeError):
        return {}
    return {r["key"]: {k: v for k, v in r.items() if k != "key"} for r in rows if r.get("key")}


def default_device(seed):
    """The seed's default device (tracked first, the focus), or None."""
    return next((k for k, v in seed.items() if v.get("default")), None)


def seed_devices(track_path, seed):
    """The first devices.json: the seed's devices, tracked as track_cfg.json had them (else the default one), with
    their calibration ids."""
    try:
        was = set(json.load(open(track_path)).get("tracked") or [])
    except (OSError, ValueError, AttributeError):
        was = set()
    was = was or ({default_device(seed)} - {None})
    out = {}
    for k, v in seed.items():
        out[k] = dict(name=v["name"], kind=v.get("kind", "tag"), role="tracked", tracked=k in was)
        if v.get("calib_id"):
            out[k]["calib_id"] = v["calib_id"]
    return out


def wap_markers(path, house):
    """The fixed WAP beacons to draw (the installation's WAP file, if it has one): those on floors the house has."""
    try:
        with open(path, encoding="utf-8") as f:
            W = json.load(f)
    except (OSError, ValueError):
        return []
    ids = list(getattr(house, "ids", []))
    return [dict(name=k, x=v["x"], y=v["y"], z_abs=v["z_abs"], floor=house.floor_of_z(v["z_abs"] - 0.01))
            for k, v in W.items() if not k.startswith("_") and isinstance(v, dict) and v.get("floor") in ids]


def ingest_token(ingest):
    """The bridge's token (RTLS_INGEST_TOKEN, else the file RTLS_INGEST_TOKEN_FILE names); a server the bridge feeds
    does not start without one."""
    token = os.environ.get("RTLS_INGEST_TOKEN")
    tf = os.environ.get("RTLS_INGEST_TOKEN_FILE", "/run/secrets/rtls_ingest_token")
    if not token and os.path.isfile(tf):
        token = open(tf).read().strip()
    if ingest is not None and not token:
        sys.exit("RTLS_SOURCE needs an ingest token: set RTLS_INGEST_TOKEN or RTLS_INGEST_TOKEN_FILE")
    return token


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


jsonable = SL.jsonable


class App:
    def __init__(self, args):
        self.args = args
        self.eng = E.Engine(log=log)
        self.cfg = dict(halflife=args.halflife, device=args.device, tracked=[args.device] if args.device else [],
                        filter=True,
                        estimator=os.environ.get("RTLS_ESTIMATOR", "trim20"),
                        motion=os.environ.get("RTLS_MOTION", "1") == "1",
                        echo_track=os.environ.get("RTLS_ECHO_TRACK", "1") == "1")
        # the device list and the focus device survive restarts: both live on the sessions volume
        self.track_path = os.path.join(args.sessions, "track_cfg.json")
        self.dev_lock = threading.Lock()
        self.devs = DV.DeviceList(os.path.join(args.sessions, "devices.json"),
                                  seed_devices(self.track_path, load_seed(SEED_PATH)))
        if self.devs.recovered:
            log("devices.json was unreadable and was set aside:", self.devs.recovered)
        self.presence = PR.Presence(os.path.join(args.sessions, "device_state.json"))
        # Running calibration (docs/specs/2026-09-27-running-calibration-design.md). off | shadow | on: shadow learns
        # and records (level events, device offsets) without changing what the tracker reports.
        self.cal_modes = dict(receivers=CB.mode("RTLS_RECEIVER_LEVELS"), devices=CB.mode("RTLS_DEVICE_OFFSETS"))
        self.rlevels = CB.ReceiverLevels(path=os.path.join(args.sessions, "level_events.json"), **CB.receiver_options())
        try:
            init = CB.initial_offsets(self.eng.known_offsets(), self.calib_ids())
        except Exception as e:                  # start from 0 rather than not start
            log("device offsets start at 0:", type(e).__name__, e)
            init = {}
        self.offsets = CB.DeviceOffsets(path=os.path.join(args.sessions, "device_offsets.json"), init=init)
        self.offsets_saved = 0.0
        # receivers switched off from Setup (Nick 2026-09-27): left out of tracking and onboarding like dead ones
        self.switches = RX.ReceiverSwitches(os.path.join(args.sessions, "receivers.json"), self.eng.scanners)
        self.last_corr = {}
        # Devices are stepped in parallel threads (Nick 2026-09-27): one core took 4-5 s a tick for 19 devices. Each
        # device's filters are its own and the model arrays are read-only; numpy releases the GIL for the heavy maths.
        n = int(os.environ.get("RTLS_STEP_THREADS", "8"))
        self.pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="step") if n > 1 else None
        log(f"running calibration: receivers {self.cal_modes['receivers']}, devices {self.cal_modes['devices']}, "
            f"{len(init)} known device offsets")
        # Outlets mode (outlet survey for the proxy placement): seeded from the outlets measured before the survey
        self.outlets = OU.OutletStore(os.path.join(args.sessions, "outlets.json"), self.eng.house,
                                      seed=os.path.join(E.SOLVER, "outlet_candidates.json"), log=log)
        if not args.replay:
            try:
                focus = json.load(open(self.track_path)).get("device")
            except (OSError, ValueError, AttributeError):
                focus = None
            tracked = self.devs.tracked_keys() or ([args.device] if args.device else [])
            self.cfg["tracked"] = tracked
            self.cfg["device"] = focus if focus in tracked else (tracked[0] if tracked else None)
        self.mode = "replay" if args.replay else os.environ.get("RTLS_SOURCE", "bermuda")
        if self.mode not in ("replay", "bermuda", "ingest", "shadow"):
            sys.exit(f"RTLS_SOURCE must be bermuda, ingest or shadow, not {self.mode!r}")
        self.shadow, self.shadow_report, self.shadow_at = None, [], 0.0
        if args.replay:
            self.src = C.ReplaySource(args.replay, self.eng.scanners, speed=args.speed, log=log)
        elif self.mode == "ingest":
            self.src = C.IngestSource(self.cfg["device"], self.eng.scanners, log=log)
        else:
            token = args.token or os.environ.get("HA_TOKEN")
            tf = args.token_file or os.environ.get("HA_TOKEN_FILE")
            if not token and tf:
                token = open(tf).read().strip()
            if not token:
                sys.exit("no Home Assistant token: set HA_TOKEN, HA_TOKEN_FILE or --token-file")
            if not args.ha_url:
                sys.exit("polling Home Assistant needs its address: set HA_URL or --ha-url")
            self.src = C.HASource(args.ha_url, token, self.cfg["device"], self.eng.scanners, log=log)
            if self.mode == "shadow":
                self.shadow = C.IngestSource(self.cfg["device"], self.eng.scanners, log=log)
        self.ingest = self.src if self.mode == "ingest" else self.shadow     # the source the bridge feeds
        self.ingest_token = ingest_token(self.ingest)
        for s in (self.src, self.shadow):
            if s is not None and hasattr(s, "set_devices"):
                s.set_devices(self.cfg["tracked"])
        if self.ingest is not None:
            self.ingest.extra_wanted = set(self.calib_ids())
        self.onboard = None if self.ingest is None else OB.Onboarding(
            self.ingest, self.devs, self.off_now, lambda: OB.HouseLocator(self.eng), log=log,
            tracked_obs=self.tracked_for_check)
        self.src.start()
        # one file an hour, closed hours gzipped, 72 h kept (sessionlog.py; Nick 2026-09-30: reduce the logging window)
        self.session = None if args.no_record else SL.SessionLog(
            args.sessions, lambda: dict(source=self.src.kind, device=self.cfg["device"], replay=args.replay,
                                        models=self.eng.models, code=self.eng.hashes, cfg=dict(self.cfg)))
        self.est_due = SL.EstThrottle()
        self.state = {"ready": False}
        self.lock = threading.Lock()
        self.tracks = {}                   # device -> model -> deque of [t, floor, x, y]
        self.truths = deque(maxlen=30)
        self.smooth = {}                   # (device, model) -> (floor, x, y) after the jitter filter
        # the focus device runs every model; the other tracked devices only this one (CPU)
        self.primary = "DominantPath" if "DominantPath" in self.eng.models else self.eng.models[0]
        self.house_store = HA.HouseStore(args.sessions)   # the map editor's draft, underlays, HA places
        self.house_store.refs = HAP.references(self.eng)  # rooms a draft must keep (the receivers, calibration)
        self.applier = HAP.Applier(args.sessions, self.house_store, before_exit=self._before_exit, log=log)
        self.house_doc = self.house_store.applied()                     # the house this process runs (apply restarts)
        self.house_hash = HD.doc_hash(self.house_doc)[:12]
        self.room_ix, self.ha_rooms = HM.room_index(self.house_doc), HM.rooms(self.house_doc)   # for Home Assistant
        self.engine_build = HD.doc_hash(self.eng.hashes)[:10]
        self.placement = PJ.PlacementJob(args.sessions, log=log)        # the placement advisor (Setup -> Outlets)
        self.house = self.build_house()
        self.landmarks = landmarks_from(self.house["fixtures"], room_of=self.eng.house.room_at)
        # ---- in-app calibration (calib.py): rounds, checks and the rig live on the sessions volume
        self.calib_dir = os.path.join(args.sessions, "calib")
        os.makedirs(os.path.join(self.calib_dir, "rounds"), exist_ok=True)
        self.calib_conv = K.ConvergenceQueue(self.calib_dir, K.subprocess_runner(ROOT, calib_dir=self.calib_dir), log=log)
        self.calib_conv.recover()
        self.calib_job = K.CaptureJob(self.src, self.eng.house, self.calib_dir, self.calib_ids,
                                      lambda: [s for s in self.eng.scanners if s not in set(self.src.dead())],
                                      model_info=dict(code=self.eng.hashes), on_keep=self.calib_conv.enqueue, log=log)
        threading.Thread(target=self.loop, daemon=True).start()

    # ----------------------------------------------------------------------------------
    def kind(self, device=None):
        return self.devs.kind(device or self.cfg["device"])

    def calib_ids(self):
        """Tag key -> short tag id shared with the older survey rounds (tag1, tag2, ...), from the device list."""
        return self.devs.calib_ids()

    def _track(self, device, m):
        return self.tracks.setdefault(device, {}).setdefault(m, deque(maxlen=60))

    def off_now(self):
        """Receivers left out right now: dead, or switched off in Setup."""
        return sorted(set(self.src.dead()) | self.switches.disabled)

    def receivers_view(self):
        """GET /api/receivers: every receiver with its switch, health, place and live corrections."""
        dead, S = set(self.src.dead()), self.eng.D.S_now
        rows = []
        for s in self.eng.scanners:
            v = S.get(s) or {}
            fi = self.eng.house.floor_of_z(v["z_abs"]) if "z_abs" in v else None
            rows.append(dict(name=s, kind="show" if E.M.is_echo(s) else "proxy", enabled=self.switches.enabled(s),
                             alive=s not in dead, room=v.get("room"), floor=fi,
                             floor_name=self.eng.floor_names[fi] if fi is not None else None,
                             correction=(self.last_corr or {}).get(s),
                             level=round(self.rlevels.level[s], 1) if s in self.rlevels.level else None))
        return dict(receivers=rows)

    def set_receiver(self, body):
        """POST /api/receivers {name, enabled}."""
        if not isinstance(body.get("enabled"), bool):
            raise ValueError("enabled must be true or false")
        self.switches.set(str(body.get("name")), body["enabled"])
        log(f"receiver {body.get('name')} switched {'on' if body['enabled'] else 'off'}")
        return self.receivers_view()

    def _step_device(self, key, win, dead, dt, echo_bias):
        """Estimate one tracked device from its window: engine step, trail, jitter filter."""
        p = self._prepare(key, win)
        return self._finish(p, self._estimate(p, dead, dt, echo_bias))

    def step_devices(self, keys, wins, dead, dt, echo_bias):
        """Every tracked device this tick: bookkeeping in order, the engine steps in the thread pool."""
        preps = [self._prepare(k, wins.get(k) or {}) for k in keys]
        run = lambda p: self._estimate(p, dead, dt, echo_bias)
        ests = list(self.pool.map(run, preps)) if self.pool is not None else [run(p) for p in preps]
        return {p["key"]: self._finish(p, e) for p, e in zip(preps, ests)}

    def _prepare(self, key, win):
        """Sequential: the window's readings, presence (which writes to disk) and a returning device's reset."""
        obs = {s: dict(med=v["med"], n=v["n"]) for s, v in win.items() if s in self.eng.scanners}
        age = self.src.device_age(key)
        seen = bool(obs) and (age is None or age < PR.AWAY_AFTER)
        if self.presence.update(key, age) == "returned":      # back after being away: a fresh room filter
            self.eng.reset_filters(key)
            self.smooth = {km: v for km, v in self.smooth.items() if km[0] != key}
        trends = [abs(v["trend"]) for v in win.values() if v.get("trend") is not None and v.get("n", 0) >= 6]
        motion = round(float(np.median(trends)), 1) if trends else None
        return dict(key=key, obs=obs, age=age, seen=seen, motion=motion, focus=key == self.cfg["device"],
                    kind=self.kind(key),
                    off_mean=self.offsets.mean(key) if self.cal_modes["devices"] == "on" else None)

    def _estimate(self, p, dead, dt, echo_bias):
        """Thread-safe: only the engine step for this device (its own filters, read-only model arrays)."""
        if not p["seen"]:
            return None
        return self.eng.step(p["obs"], dead, dt, device_halflife(p["kind"], self.cfg["halflife"]), kind=p["kind"],
                             use_filter=device_filter(p["kind"], self.cfg["filter"]), motion=self.cfg["motion"],
                             k_adj=echo_bias, motion_db=p["motion"], device=p["key"],
                             models=None if p["focus"] else [self.primary], off_mean=p["off_mean"])

    def _finish(self, p, est):
        """Sequential: offset learning, trails, the jitter filter and presence notes."""
        t = time.time()
        key, obs, age, seen, motion = p["key"], p["obs"], p["age"], p["seen"], p["motion"]
        if est and self.cal_modes["devices"] != "off":
            CB.learn(self.offsets, key, est.get(self.primary), motion, t)
        if est:
            for m in self.eng.models:
                e = est.get(m)
                if e:
                    self._track(key, m).append([round(t, 1), e["map_floor"], e["xy"][0], e["xy"][1]])
        # ---- motion-aware jitter filter + confidence sphere ----
        # still (~1 dB) -> heavy smoothing; walking (>= 6 dB) -> track it
        alpha = 0.15 if motion is None else float(min(0.85, max(0.15, 0.15 + 0.12 * motion)))
        for m, e in (est or {}).items():
            if not isinstance(e, dict) or not e.get("xy"):
                continue
            fi, (x, y) = e["map_floor"], e["xy"]
            # Room-graph constraint: if the HMM is confident and the raw best cell lies in a
            # different room, aim at the best cell inside the believed room instead.
            if e.get("room_xy") and e.get("room_xy_room") and e.get("room") != e["room_xy_room"]:
                fi, (x, y) = e["room_xy_floor"], e["room_xy"]
                e["snapped_to_room"] = e["room_xy_room"]
            prev = self.smooth.get((key, m))
            if prev and prev[0] == fi:
                x = prev[1] + alpha * (x - prev[1]); y = prev[2] + alpha * (y - prev[2])
            self.smooth[(key, m)] = (fi, x, y)
            e["display_xy"] = [round(x, 2), round(y, 2)]
            e["motion_db"] = motion
            e["sphere_r"] = round(float(e.get("r68", 1.0)) * (1.0 + (motion or 0.0) / 4.0), 2)
        e0 = (est or {}).get(self.primary)
        if isinstance(e0, dict) and e0.get("room"):
            self.presence.note(key, e0["room"], e0.get("floor_name"))
        return dict(obs=obs, age=age, seen=seen, motion=motion, est=est)

    def _tracked_row(self, key, r):
        """Compact per-device summary for the tracking list and the extra markers."""
        e = (r["est"] or {}).get(self.primary)
        lite = None
        if isinstance(e, dict) and e.get("xy"):
            f = (e.get("filtered") or [None])[0]
            lite = {k: e.get(k) for k in ("room", "floor", "floor_name", "p_room", "verdict", "map_floor",
                                          "display_xy", "xy", "z", "r68", "warming", "raw", "gated")}   # raw: the one-window call
            px, py = lite["display_xy"] or lite["xy"]
            lite["near"] = nearest_landmark(self.landmarks, lite["map_floor"], px, py, room=lite["room"])
            lite["description"] = describe(lite["room"], lite["near"])
            lite["filtered"] = f
        return dict(key=key, label=self.devs.name(key, key), kind=self.kind(key), focus=key == self.cfg["device"],
                    seen=r["seen"], age=r["age"], est=lite, trail=list(self._track(key, self.primary)),
                    **self.presence.row(key))

    def loop(self):
        last = time.time()
        while True:
            t = time.time()
            dt, last = t - last, t
            try:
                focus = self.cfg["device"]
                keys = ([focus] if focus else []) + [k for k in self.cfg["tracked"] if k != focus]
                wins = {k: self.src.window(device_halflife(self.kind(k), self.cfg["halflife"]), self.cfg["estimator"], device=k) for k in keys}
                win = wins.get(focus) or {}
                dead = self.src.dead()
                off = sorted(set(dead) | self.switches.disabled)       # dead, or switched off in Setup
                # Differential anchor correction: every scanner, every tick, relative to its own
                # long-run anchor medians. Applies to all models identically (no prediction in the loop).
                if self.cfg["echo_track"]:
                    corr = self.eng.anchor_correction(self.src.anchor_stats())
                    if self.cal_modes["receivers"] != "off":
                        self.rlevels.update({s: (d or {}).get("raw") for s, d in (self.eng.anchor_detail or {}).items()}, t)
                        if self.cal_modes["receivers"] == "on":
                            corr = self.rlevels.correction(E.M.is_echo, instant=corr)
                    echo_bias = {m: corr for m in self.eng.models}
                    self.last_corr = corr
                else:
                    corr, echo_bias = None, None
                res = self.step_devices(keys, wins, off, dt, echo_bias)
                # no focus before the first device is onboarded (a new household): nothing to estimate yet
                r0 = res[focus] if focus in res else dict(est={}, age=None, seen=False, motion=None)
                est, age, seen, motion = r0["est"], r0["age"], r0["seen"], r0["motion"]
                tracked = [self._tracked_row(k, res[k]) for k in self.cfg["tracked"] if k in res]
                st = dict(ready=True, t=t, tick=TICK, cfg=self.cfg, device_label=self.devs.name(focus, "?"),
                          devices={k: dict(name=v["name"], kind=v["kind"], role=v["role"], tracked=v["tracked"])
                                   for k, v in self.devs.devices.items()},
                          devices_recovered=self.devs.recovered,
                          motion_db=motion, echo_bias=echo_bias,
                          anchor_corr=corr, anchor_detail=getattr(self.eng, "anchor_detail", None),
                          source=self.src.status(), dead=dead, disabled=sorted(self.switches.disabled),
                          device_age=age, seen=seen,
                          window={s: win[s] for s in sorted(win) if s in self.eng.scanners},
                          scanners=self.eng.scanners, estimates=est,
                          tracks={m: list(self._track(focus, m)) if focus else [] for m in self.eng.models},
                          tracked=tracked,
                          truths=list(self.truths), session=self.session.name if self.session else None,
                          code=self.eng.hashes,
                          calibration=dict(modes=self.cal_modes,
                                           receiver_levels={s: round(v, 1) for s, v in self.rlevels.level.items()},
                                           level_events=self.rlevels.events[-10:],
                                           device_offsets={k: round(self.offsets.mean(k), 1) for k in self.cfg["tracked"]}))
                if self.cal_modes["devices"] != "off" and t - self.offsets_saved >= 60.0:
                    self.offsets.save()
                    self.offsets_saved = t
                if self.shadow is not None and t - self.shadow_at >= 60.0:
                    self.shadow_at = t
                    self.shadow_report = C.compare_sources(self.src, self.shadow, self.cfg["tracked"])
                    if self.session:
                        self.session.write("shadow", dict(rows=self.shadow_report))
                with self.lock:
                    self.state = jsonable(st)
                if self.session:
                    pend = self.src.take_pending_all()
                    if pend.get(focus):
                        self.session.write("raw", dict(samples=pend[focus]))
                    if pend:                       # every tracked device, for the replay evaluator
                        self.session.write("raw_all", dict(devices=pend))
                    lite = None
                    if est:
                        lite = {m: ({k: e[k] for k in ("floor", "room", "p_room", "p_floor", "verdict", "xy",
                                                        "map_floor", "r68")} | ({"filtered": e["filtered"][0]} if e.get("filtered") else {}))
                                if e and "xy" in e else e for m, e in est.items()}
                    others = {row["key"]: {k: (row["est"] or {}).get(k) for k in ("room", "floor", "p_room", "xy",
                                                                                     "map_floor", "r68")}
                              for row in tracked if not row["focus"] and row["est"]}
                    sig = (tuple(off), tuple((row["key"], (row["est"] or {}).get("room")) for row in tracked))
                    if self.est_due.due(t, sig):        # the config is in each hour's header and in config records
                        self.session.write("est", dict(window={s: [v["med"], v["n"]] for s, v in win.items()},
                                                       dead=off, device_age=age,
                                                       bermuda_live=self.src.status().get("device_info", {}),
                                                       estimates=lite, others=others or None))
            except Exception as e:
                tb = traceback.extract_tb(e.__traceback__)[-1]          # where: a one-tick glitch has to name itself
                log("loop error:", type(e).__name__, e, f"at {os.path.basename(tb.filename)}:{tb.lineno} ({tb.name})")
            time.sleep(max(0.2, TICK - (time.time() - t)))

    # ---- the placement advisor (spec 2026-10-01): one background run, the last result -----------------------------
    def placement_status(self):
        return self.placement.status(self.house_hash, PJ.outlets_key(self.outlets.view()["outlets"]))

    def placement_start(self, body):
        outlets = self.outlets.view()["outlets"]
        return self.placement.start(body, outlets, self.house_hash, PJ.outlets_key(outlets))

    # ----------------------------------------------------------------------------------
    def _before_exit(self):
        """An apply is about to restart the process: close the session log so its last records are on disk."""
        if self.session is not None:
            self.session.close()

    def build_house(self):
        H = self.eng.house
        out = HA.house_payload(H, self.eng.floor_names)          # the geometry (shared with the map editor's preview)
        scanners = []
        # draw where the scanners are NOW (D.S holds capture-time geometry, used only for fitting)
        for s, v in self.eng.D.S_now.items():
            fi = H.floor_of_z(v["z_abs"])
            scanners.append(dict(name=s, x=v["x"], y=v["y"], z_abs=v["z_abs"], floor=fi, room=v.get("room"),
                                 kind="echo" if "Show" in s else "esp32",
                                 moved_at=v.get("moved_at")))
        waps = wap_markers(C.WAPS_FILE, H)
        return jsonable(dict(out,
                             scanners=scanners, waps=waps, devices={k: v["name"] for k, v in self.devs.devices.items()},
                             estimators={k: v[0] for k, v in C.ESTIMATORS.items()},
                             graph=[[list(a), list(b)] for a, nb in self.eng.graph.items() for b in nb if a < b],
                             models=self.eng.models, code=self.eng.hashes, fit=self.eng.trained))

    # ----------------------------------------------------------------------------------

    # ---- calibration -------------------------------------------------------------------
    def calib_rig(self, rig=None, warnings=None):
        rig = rig or K.load_rig(self.calib_dir)
        ids, conf = self.calib_ids(), set(getattr(self.src, "configured_keys", set()))
        if warnings is None:
            try:
                warnings = K.validate_rig(rig, ids, conf)[1]
            except ValueError as e:
                warnings = [{"warning": str(e)}]
        return dict(rig=rig, warnings=warnings, locked=self.calib_job.locked,
                    tags={k: dict(id=v, label=self.devs.name(k, v), configured=k in conf) for k, v in ids.items()})

    def calib_rig_put(self, body):
        if self.calib_job.locked:
            raise K.Busy("the rig is locked while a capture is running")
        rig, warnings = K.save_rig(self.calib_dir, body.get("rig") or body, self.calib_ids(),
                                   set(getattr(self.src, "configured_keys", set())))
        return self.calib_rig(rig, warnings)

    def calib_coverage(self):
        H = self.eng.house
        scanners = {s: dict(x=v["x"], y=v["y"], z_abs=v["z_abs"], floor=H.floor_of_z(v["z_abs"]))
                    for s, v in self.eng.D.S_now.items()}
        latest = self.calib_conv.status().get("latest") or {}
        flagged = {(x["round"], x["tag"]) for x in (latest.get("surprises") or [])}
        return K.coverage(K.load_kept_rounds(self.calib_dir), scanners, flagged)

    def truth(self, body):
        fi = int(body["floor"])
        x, y = body.get("x"), body.get("y")
        room = body.get("room") or (self.eng.house.room_at(fi, float(x), float(y)) if x is not None else None)
        with self.lock:
            est = (self.state.get("estimates") or {})
        snap = {m: dict(room=e.get("room"), floor=e.get("floor"), xy=e.get("xy"), verdict=e.get("verdict"))
                for m, e in est.items() if isinstance(e, dict) and "room" in e}
        rec = dict(floor=fi, x=x, y=y, room=room, note=body.get("note"), estimates_at_label=snap, t=time.time())
        self.truths.append(jsonable(rec))
        if self.session:
            self.session.write("truth", rec)
        return rec

    def _set_tracking(self, focus, tracked):
        """Apply a focus device + tracked set to the collector and engine, and persist it (flags in the device
        list, the focus in track_cfg.json)."""
        self.cfg["device"], self.cfg["tracked"] = focus, list(tracked)
        self.src.set_device(focus)
        self.src.set_devices(tracked)
        if self.shadow is not None:
            self.shadow.set_device(focus)
            self.shadow.set_devices(tracked)
        self.eng.drop_devices(keep=set(tracked))
        for k in list(self.tracks):
            if k not in tracked:
                del self.tracks[k]
        self.smooth = {km: v for km, v in self.smooth.items() if km[0] in tracked}
        try:
            K.write_json_atomic(self.track_path, dict(device=focus))
        except OSError as e:
            log("could not save the tracked set:", e)

    def _retrack(self):
        """Follow the device list after an edit: its tracked flags, keeping the focus when it is still tracked."""
        tracked = self.devs.tracked_keys()
        self._set_tracking(self.cfg["device"] if self.cfg["device"] in tracked else (tracked[0] if tracked else None),
                           tracked)

    def _refuse_last(self, key):
        if self.devs.tracked_keys() == [key]:
            raise DV.DeviceError("track at least one device")

    def device_add(self, body):
        with self.dev_lock:
            key = DV.check_key(body.get("key"))
            e = self.devs.add(key, body.get("name"), body.get("kind", "tag"), body.get("role", "tracked"))
            self._retrack()
            return dict(key=key, **e)

    def device_patch(self, key, body):
        with self.dev_lock:
            key = DV.check_key(key)
            self.devs.get(key)
            if body.get("role") == "fixed":
                self._refuse_last(key)
            e = self.devs.update(key, body)
            self._retrack()
            return dict(key=key, **e)

    def device_delete(self, key):
        with self.dev_lock:
            key = DV.check_key(key)
            self.devs.get(key)
            self._refuse_last(key)
            self.devs.remove(key)
            self.presence.forget(key)
            self._retrack()
            return dict(key=key, removed=True)

    def configure(self, body):
        if "halflife" in body:
            self.cfg["halflife"] = float(min(120.0, max(2.0, float(body["halflife"]))))
        if "filter" in body:
            self.cfg["filter"] = bool(body["filter"])
        if body.get("estimator") in C.ESTIMATORS:
            self.cfg["estimator"] = body["estimator"]
        if "motion" in body:
            self.cfg["motion"] = bool(body["motion"])
            self.eng.reset_filters()
        if "echo_track" in body:
            self.cfg["echo_track"] = bool(body["echo_track"])
        # every tracked-role device is tracked (2026-09-26): a "tracked" list in the body is ignored
        trackable = {k for k, v in self.devs.devices.items() if v["role"] == "tracked"}
        if "device" in body and body["device"] != self.cfg["device"]:
            if body["device"] not in trackable:
                raise ValueError(f"unknown device {body['device']!r}")
            tracked = self.cfg["tracked"] + ([body["device"]] if body["device"] not in self.cfg["tracked"] else [])
            self._set_tracking(body["device"], tracked)
        if body.get("reset_filter"):
            self.eng.reset_filters(self.cfg["device"])
        if self.session:
            self.session.write("config", dict(cfg=self.cfg))
        return self.cfg

    def tracked_for_check(self):
        """Tracked devices that are present, with their own readings and the tracker's room: onboarding scores its
        locator against these on real data."""
        with self.lock:
            rows = list(self.state.get("tracked") or [])
        out = []
        for r in rows:
            e = r.get("est") or {}
            if r.get("status") != "present" or not e.get("room"):
                continue
            win = self.src.window(device_halflife(self.kind(r["key"]), self.cfg["halflife"]), self.cfg["estimator"], device=r["key"])
            per = {s: (v["med"], v["n"]) for s, v in win.items() if s in self.eng.scanners}
            out.append(dict(key=r["key"], name=r["label"], per=per, room=e["room"], floor=e.get("floor_name")))
        return out

    def ingest_reply(self, meta=False):
        """What the bridge gets back for a batch: the keys to send next time, and the current answers - with the map's
        room and floor ids and the linked Home Assistant area per device; and, when the bridge asks (about every 10 s),
        the meta: the engine's status, the map's rooms and their area links, the receivers (spec 2026-10-01-ha-ui)."""
        with self.lock:
            rows = list(self.state.get("tracked") or [])
        tracked = []
        for r in rows:
            e = r.get("est") or {}
            xy = e.get("display_xy") or e.get("xy") or [None, None]
            fi = e.get("floor")
            fid = self.eng.house.ids[fi] if isinstance(fi, int) and 0 <= fi < len(self.eng.house.ids) else None
            link = self.room_ix.get((fid, e.get("room"))) or {}
            tracked.append(dict(key=r["key"], name=r["label"], room=e.get("room"), floor=e.get("floor_name"),
                                p_room=e.get("p_room"), x=xy[0], y=xy[1], z=e.get("z"), r68=e.get("r68"),
                                verdict=e.get("verdict"), age=r.get("age"), near=e.get("near"),
                                description=e.get("description"), status=r.get("status"),
                                last_seen=r.get("last_seen"), last_room=r.get("last_room"),
                                last_floor=r.get("last_floor"), room_id=link.get("room_id"),
                                floor_id=fid if e.get("room") else None, area=link.get("area")))
        out = dict(wanted=self.ingest.wanted(), tracked=tracked, removed=self.devs.tombstones(),
                   census_detail=bool(self.onboard and self.onboard.active()))
        if meta:
            out["meta"] = dict(engine=dict(build=self.engine_build, status=HM.status(self.applier.status().get("state"))),
                               rooms=self.ha_rooms, receivers=HM.receivers(self.receivers_view()["receivers"], self.house_doc))
        return out


house_ready = HAP.house_ready
SETUP_HINT = {"rooms": "the house has no rooms yet: draw them in Setup -> Map, then Apply",
              "receivers": "no receiver is placed yet: place the ones Home Assistant hears in Setup -> Map, then Apply"}


class SetupApp:
    """A house the engine cannot run on yet - no room, or no placed receiver: a new household's first start. No
    engine: the map editor, Apply and its history, underlays, Home Assistant's places and the bridge's ingest work (the
    receivers Home Assistant hears are what the editor places); every other route answers 503 {setup: true}."""
    setup = True

    def __init__(self, args, missing):
        self.args, self.missing = args, missing
        self.mode = "replay" if args.replay else os.environ.get("RTLS_SOURCE", "bermuda")
        self.ingest = C.IngestSource(None, [], log=log) if self.mode in ("ingest", "shadow") else None
        self.src, self.session, self.shadow_report = self.ingest, None, []
        self.ingest_token = ingest_token(self.ingest)
        self.house_store = HA.HouseStore(args.sessions)
        self.applier = HAP.Applier(args.sessions, self.house_store, log=log)
        self.house_doc = self.house_store.applied()
        self.lock = threading.Lock()
        self.state = dict(ready=False, setup=True, missing=missing)
        self.house = dict(setup=True, missing=missing, floors=[f.get("name") for f in self.house_doc["floors"]])
        log("setup mode:", SETUP_HINT[missing])

    def refusal(self):
        return dict(setup=True, missing=self.missing, error=SETUP_HINT[self.missing])

    def ingest_reply(self, meta=False):
        out = dict(wanted=self.ingest.wanted(), tracked=[], removed=[], census_detail=False)
        if meta:
            out["meta"] = dict(engine=dict(build=None, status=HM.status(self.applier.status().get("state"), idle="setup")),
                               rooms=HM.rooms(self.house_doc), receivers=[])
        return out


def start_app(args):
    """The server for the house the data folder holds: the engine's when the house has a room and a placed receiver,
    else setup mode. A house file that cannot be read raises - after an apply, the boot guard puts the previous back."""
    ready, missing = house_ready(HD.load())
    return App(args) if ready else SetupApp(args, missing)


# what answers in setup mode, by method: the page, the map editor, Apply and its history, the bridge
SETUP_ROUTES = {
    "GET": re.compile(r"/|/index\.html|/static/.+|/api/(state|sessions(/.+)?|ha/places|devices/seen|ingest/(hello|compare)"
                      r"|house(/doc|/draft|/draft/problems|/draft/preview|/apply|/history|/underlay/.+)?)"),
    "PUT": re.compile(r"/api/house/draft"),
    "DELETE": re.compile(r"/api/house/draft"),
    "POST": re.compile(r"/api/(ingest|house/(underlay|apply|restore))"),
}


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", cache="no-store"):
            data = body if isinstance(body, bytes) else json.dumps(body, separators=(",", ":")).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(data)

        def _calib(self, fn, *a):
            try:
                return self._send(200, jsonable(fn(*a)))
            except K.Busy as e:
                return self._send(409, {"error": str(e)})
            except (KeyError, ValueError, TypeError) as e:
                return self._send(400, {"error": str(e)})

        def _devices(self, fn, *a):
            try:
                return self._send(200, jsonable(fn(*a)))
            except DV.DeviceExists as e:
                return self._send(409, {"error": str(e)})
            except DV.NoSuchDevice as e:
                return self._send(404, {"error": f"no such device: {e.args[0]}"})
            except (DV.DeviceError, ValueError, TypeError) as e:
                return self._send(400, {"error": str(e)})

        def _outlets(self, fn, *a):
            try:
                return self._send(200, jsonable(fn(*a)))
            except OU.NoSuchOutlet as e:
                return self._send(404, {"error": f"no such outlet: {e.args[0]}"})
            except (OU.OutletError, ValueError, TypeError) as e:
                return self._send(400, {"error": str(e)})

        def _length(self):
            """The request's Content-Length; ValueError when it is not a whole number of bytes."""
            v = self.headers.get("Content-Length") or "0"
            if not v.strip().isdigit():
                raise ValueError("bad Content-Length")
            return int(v)

        def _too_big(self, n, msg):
            """A 413 the client can read: the body is drained first (up to 64 MB), else the client sees a reset."""
            left = n if n <= 64 * 2**20 else 0
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
            self.close_connection = True
            return self._send(413, {"error": msg})

        def _body(self):
            n = self._length()
            return json.loads(self.rfile.read(n) or b"{}")

        def _setup_refuses(self):
            """In setup mode, a route that needs the engine is answered 503 {setup: true, missing, error} (its body
            read first, so the client sees the answer): True when it was."""
            if not getattr(app, "setup", False):
                return False
            rx = SETUP_ROUTES.get(self.command)
            if rx is not None and rx.fullmatch(self.path.split("?")[0]):
                return False
            try:
                n = self._length()
            except ValueError:
                n = 0
            if 0 < n <= MAX_INGEST:
                self.rfile.read(n)
            elif n:
                self.close_connection = True
            self._send(503, app.refusal())
            return True

        def _authed(self):
            got = self.headers.get("Authorization", "")
            return bool(app.ingest_token) and hmac.compare_digest(got, "Bearer " + app.ingest_token)

        def _ingest(self):
            if app.ingest is None:
                return self._send(503, {"error": "ingest disabled (RTLS_SOURCE=bermuda)"})
            if not self._authed():
                return self._send(401, {"error": "bad token"})
            try:
                n = self._length()
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            if n > MAX_INGEST:
                if n <= 4 * MAX_INGEST:
                    self.rfile.read(n)          # drain, so the client gets the 413 instead of a reset
                self.close_connection = True
                return self._send(413, {"error": "batch too large"})
            try:
                batch = json.loads(self.rfile.read(n) or b"{}")
                app.ingest.push(batch)
            except (ValueError, KeyError, TypeError, IndexError) as e:
                return self._send(400, {"error": f"bad batch: {e}"})
            if isinstance(batch, dict) and isinstance(batch.get("places"), dict):
                app.house_store.set_places(batch["places"])   # HA's floors and areas for the map editor (bridge 0.3)
            return self._send(200, jsonable(app.ingest_reply(meta=isinstance(batch, dict) and batch.get("meta") is True)))

        def _house_body(self):
            """A JSON body of up to MAX_HOUSE bytes (a house document); ValueError when not JSON."""
            return json.loads(self.rfile.read(self._length()) or b"null")

        def _house(self, fn, *a):
            try:
                return self._send(200, jsonable(fn(*a)))
            except ValueError as e:
                return self._send(400, {"error": str(e)})

        def do_PUT(self):
            if self._setup_refuses():
                return
            path = self.path.split("?")[0]
            if path == "/api/house/draft":
                try:
                    if self._length() > MAX_HOUSE:
                        return self._too_big(self._length(), f"a house document may be at most {MAX_HOUSE // 2**20} MB")
                    body = self._house_body()
                except ValueError as e:
                    return self._send(400, {"error": str(e) if "Content-Length" in str(e) else "bad json"})
                return self._house(app.house_store.save_draft, body)
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "bad json"})
            if path == "/api/calib/rig":
                return self._calib(app.calib_rig_put, body)
            return self._send(404, {"error": "not found"})

        def do_PATCH(self):
            if self._setup_refuses():
                return
            m = re.fullmatch(r"/api/devices/([0-9A-Za-z:_]+)", self.path)
            o = re.fullmatch(r"/api/outlets/([0-9A-Za-z_\-]+)", self.path)
            if not (m or o):
                return self._send(404, {"error": "not found"})
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "bad json"})
            if o:
                return self._outlets(app.outlets.update, o.group(1), body)
            return self._devices(app.device_patch, m.group(1), body)

        def do_DELETE(self):
            if self._setup_refuses():
                return
            if self.path.split("?")[0] == "/api/house/draft":
                app.house_store.discard_draft()
                return self._send(200, {"discarded": True})
            if self.path.split("?")[0] == "/api/placement":
                return self._send(200, jsonable(app.placement.cancel()))
            o = re.fullmatch(r"/api/outlets/([0-9A-Za-z_\-]+)", self.path)
            if o:
                return self._outlets(app.outlets.remove, o.group(1))
            m = re.fullmatch(r"/api/devices/([0-9A-Za-z:_]+)", self.path)
            if not m:
                return self._send(404, {"error": "not found"})
            return self._devices(app.device_delete, m.group(1))

        def do_GET(self):
            if self._setup_refuses():
                return
            p, _, qs = self.path.partition("?")
            if p == "/api/ingest/hello":
                if app.ingest is None:
                    return self._send(503, {"error": "ingest disabled (RTLS_SOURCE=bermuda)"})
                if not self._authed():
                    return self._send(401, {"error": "bad token"})
                return self._send(200, {"v": 1, "ok": True, "mode": app.mode})
            if p == "/api/ingest/compare":
                return self._send(200, {"mode": app.mode, "rows": jsonable(app.shadow_report),
                                        "unplaced": dict(app.ingest.unplaced) if app.ingest else {}})
            if p == "/api/devices/seen":
                src = app.ingest
                return self._send(200, jsonable({"at": src.census_at if src else None,
                                                 "devices": src.census if src else []}))
            if p == "/api/snapshot":
                snap = getattr(app.src, "snapshot", None)
                if snap is None:
                    return self._send(503, {"error": "no sample buffers in replay mode"})
                q = dict(kv.split("=", 1) for kv in qs.split("&") if "=" in kv)
                try:
                    secs = min(C.KEEP, max(10.0, float(q.get("seconds", C.KEEP))))
                except ValueError:
                    return self._send(400, {"error": "seconds must be a number"})
                now = app.src.now() or time.time()
                return self._send(200, jsonable(dict(t0=round(now - secs, 1), seconds=secs, data=snap(secs, now))))
            if p == "/api/onboarding":
                if app.onboard is None:
                    return self._send(503, {"error": "onboarding needs the RTLS@Home bridge (RTLS_SOURCE=ingest or shadow)"})
                return self._send(200, jsonable(app.onboard.view()))
            if p == "/api/devices":
                return self._send(200, jsonable(app.devs.view()))
            if p == "/api/outlets":
                return self._send(200, jsonable(app.outlets.view()))
            if p == "/api/receivers":
                return self._send(200, jsonable(app.receivers_view()))
            if p == "/api/placement":
                return self._send(200, jsonable(app.placement_status()))
            if p == "/api/calib/rig":
                return self._calib(app.calib_rig)
            if p == "/api/calib/status":
                job = dict(kv.split("=", 1) for kv in qs.split("&") if "=" in kv).get("job")
                return self._calib(app.calib_job.status, job)
            if p == "/api/calib/coverage":
                return self._calib(app.calib_coverage)
            if p == "/api/calib/convergence":
                return self._calib(app.calib_conv.status)
            if p in ("/", "/index.html"):
                p = "/static/index.html"
            if p == "/api/house":
                return self._send(200, app.house)
            if p == "/api/house/doc":
                return self._house(app.house_store.applied)
            if p == "/api/house/draft":
                return self._house(app.house_store.state)
            if p == "/api/house/apply":
                return self._send(200, jsonable(app.applier.status()))
            if p == "/api/house/history":
                return self._send(200, {"versions": HAP.history(app.args.sessions)})
            if p == "/api/house/draft/problems":
                return self._house(app.house_store.problems)
            if p == "/api/house/draft/preview":
                return self._send(200, HA.preview(app.house_store.draft(), {}))
            if p == "/api/ha/places":
                return self._send(200, app.house_store.places())
            m = re.fullmatch(r"/api/house/underlay/([A-Za-z0-9_\-]+\.(png|jpg|webp))", p)
            if m:
                fp = app.house_store.underlay_file("underlays/" + m.group(1))
                if fp is None:
                    return self._send(404, {"error": "no such underlay"})
                named = re.search(r"-[0-9a-f]{8}\.\w+$", m.group(1))      # named by its content: never changes
                return self._send(200, open(fp, "rb").read(), HA.CONTENT_TYPE[m.group(2)],
                                  "public, max-age=31536000, immutable" if named else "no-cache")
            if p == "/api/state":
                with app.lock:
                    return self._send(200, app.state)
            if p == "/api/sessions":
                fs = sorted(os.listdir(app.args.sessions), reverse=True) if os.path.isdir(app.args.sessions) else []
                return self._send(200, [dict(name=f, bytes=os.path.getsize(os.path.join(app.args.sessions, f)))
                                        for f in fs if f.endswith((".jsonl", ".jsonl.gz"))])
            m = re.fullmatch(r"/api/sessions/([A-Za-z0-9_.\-]+\.jsonl(\.gz)?)", p)
            if m:
                fp = os.path.join(app.args.sessions, m.group(1))
                if os.path.isfile(fp):
                    return self._send(200, open(fp, "rb").read(), "application/gzip" if m.group(2) else "application/x-ndjson")
                return self._send(404, {"error": "no such session"})
            fp = static_file(p)
            if fp:
                ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
                return self._send(200, open(fp, "rb").read(), ctype)
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if self._setup_refuses():
                return
            if self.path == "/api/ingest":
                return self._ingest()
            try:
                n = self._length()
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            if self.path.split("?")[0] == "/api/house/underlay":
                if n > MAX_HOUSE:
                    return self._too_big(n, f"an image may be at most {HA.MAX_UNDERLAY // 2**20} MB")
                q = dict(x.split("=", 1) for x in self.path.partition("?")[2].split("&") if "=" in x)
                data = self.rfile.read(n)
                return self._house(lambda: {"file": app.house_store.save_underlay(q.get("floor"),
                                                                                  self.headers.get("Content-Type"), data)})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            if self.path == "/api/house/apply":
                try:
                    return self._send(202, jsonable(app.applier.start()))
                except HAP.ProblemsError as e:
                    return self._send(400, jsonable({"error": str(e), "problems": e.problems}))
                except HAP.Busy as e:
                    return self._send(409, jsonable({"error": str(e), "status": app.applier.status()}))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if self.path == "/api/placement":
                try:
                    return self._send(202, jsonable(app.placement_start(body)))
                except PJ.Busy as e:
                    return self._send(409, jsonable({"error": str(e), "status": app.placement_status()}))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if self.path == "/api/house/restore":
                return self._house(app.applier.restore, body.get("name") if isinstance(body, dict) else None)
            if self.path == "/api/devices":
                return self._devices(app.device_add, body)
            if self.path == "/api/outlets":
                return self._outlets(app.outlets.add, body)
            if self.path == "/api/devices/dismiss_recovered":
                app.devs.recovered = None
                return self._send(200, {"ok": True})
            calib = {"/api/calib/capture": app.calib_job.start, "/api/calib/keep": lambda b: app.calib_job.keep(),
                     "/api/calib/redo": lambda b: app.calib_job.redo(), "/api/calib/cancel": lambda b: app.calib_job.cancel()}
            if self.path in calib:
                return self._calib(calib[self.path], body)
            try:
                if self.path == "/api/truth":
                    return self._send(200, app.truth(body))
                if self.path == "/api/receivers":
                    return self._send(200, jsonable(app.set_receiver(body)))
                if self.path == "/api/config":
                    return self._send(200, app.configure(body))
            except (KeyError, ValueError, TypeError) as e:
                return self._send(400, {"error": str(e)})
            return self._send(404, {"error": "not found"})
    return H


STARTING_PAGE = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta http-equiv="refresh" content="3"><title>RTLS@Home</title>
<style>body{margin:0;height:100vh;display:grid;place-items:center;background:#0f141b;color:#e6edf5;
font:15px system-ui,sans-serif}p{color:#8b98a9;max-width:28em}</style></head>
<body><div><h2>RTLS@Home is starting</h2><p>The engine is loading the house. The first start builds its maps and
takes about half a minute; this page reloads by itself.</p></div></body></html>"""


def starting_handler():
    """What the port answers while the engine is being built: the page above at the root, 503 elsewhere (the
    bridge backs off, the viewer's requests fail and retry)."""
    class Starting(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if 0 < n <= MAX_INGEST:
                self.rfile.read(n)                              # drain a batch, so the client sees the 503
            elif n:
                self.close_connection = True
            root = self.path.split("?")[0] in ("/", "/index.html")
            body = STARTING_PAGE if root else b'{"starting": true, "error": "the engine is starting"}'
            self.send_response(200 if root else 503)
            self.send_header("Content-Type", "text/html; charset=utf-8" if root else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Retry-After", "3")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = _reply

    return Starting


def serve(host, port, start_app, handler_for):
    """Binds at once and answers "starting" while start_app() builds the engine - through Home Assistant's ingress a
    closed port is a bare 502, and the App's watchdog checks the port - then hands every request to handler_for(app).
    Returns (server, app, the serving thread)."""
    srv = ThreadingHTTPServer((host, port), starting_handler())
    t = threading.Thread(target=srv.serve_forever, daemon=True, name="http")
    t.start()
    app = start_app()
    srv.RequestHandlerClass = handler_for(app)
    return srv, app, t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("RTLS_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("RTLS_PORT", "8765")))
    ap.add_argument("--ha-url", default=os.environ.get("HA_URL"))
    ap.add_argument("--token", default=None)
    ap.add_argument("--token-file", default=None)
    ap.add_argument("--device", default=os.environ.get("RTLS_DEVICE") or default_device(load_seed(SEED_PATH)))
    ap.add_argument("--halflife", type=float, default=float(os.environ.get("RTLS_HALFLIFE", "15")))
    ap.add_argument("--sessions", default=os.environ.get("RTLS_SESSIONS", os.path.join(ROOT, "app", "sessions")))
    ap.add_argument("--replay", default=None, help="play a recorded capture instead of polling HA")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--no-record", action="store_true")
    args = ap.parse_args()
    # the data folder is where the applied house, its builds and caches live (house_doc, house_build, geom3d)
    os.environ["RTLS_SESSIONS"] = os.path.abspath(args.sessions)
    # the first start after an apply is checked: if the server cannot start on the new house, the previous one goes
    # back and the process restarts (spec 2026-09-30 section 5)
    srv, app, http = serve(args.host, args.port, lambda: HAP.boot(args.sessions, lambda: start_app(args), log=log),
                           make_handler)
    log(f"serving http://{args.host}:{args.port}  source={getattr(app.src, 'kind', None)}  "
        f"session={app.session.name if app.session else None}")
    http.join()


if __name__ == "__main__":
    main()
