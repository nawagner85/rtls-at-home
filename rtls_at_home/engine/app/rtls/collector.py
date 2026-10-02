"""Data sources for the live viewer: Home Assistant (Bermuda) or replay of a recorded capture.

Both keep per-scanner sample buffers and reduce them to an exponentially weighted MEDIAN
(half-life adjustable): smooth like a moving average, but a one-off 10 dB jump from the
two-state RSSI we see on short paths can't drag it around.

Health: a scanner missing from the configured-device dump is offline; a deep check every
60 s (full dump) flags scanners that are connected but have relayed nothing - the silent
failure seen on one proxy. Dead scanners are dropped, never read as "far away".
"""
import json
import threading
import time
import urllib.request
import os
from collections import defaultdict, deque

import numpy as np

DEAD_AFTER = 60.0        # s without relaying any advert from any device
KEEP = 300.0             # s of samples kept per scanner


def scanner_name(n):
    return (n or "?").split(" (")[0].strip()


def _mac_plus(mac, k=2):
    """ESP32 BLE address = Wi-Fi MAC + 2 in the last octet."""
    try:
        b = [int(x, 16) for x in mac.split(":")]
        b[-1] = (b[-1] + k) & 0xFF
        return ":".join("%02x" % x for x in b)
    except Exception:
        return None


def load_addr_names():
    """Scanner address (Wi-Fi MAC and BLE MAC) -> configured scanner name, from the house's receivers (or
    scanners_v2.json for a house without them).
    Bermuda labels a scanner with its HA device name, which is a startup race: after the HA restart
    of 2026-09-23 five ESP32 proxies came back labelled by Wi-Fi MAC ('00-00-5E-00-53-01') and a
    name-keyed collector declared them dead while they relayed normally. Addresses never change."""
    out = {}
    try:
        import sys
        solver = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "solver")
        if solver not in sys.path:
            sys.path.insert(0, solver)
        import house_receivers as HR
        for name, v in HR.registry().items():
            mac = (v.get("mac") or "").lower()
            if mac:
                out[mac] = name
                out[_mac_plus(mac)] = name
            src = (v.get("ble_source") or "").lower()      # scanners without a Wi-Fi MAC (the Echo Shows):
            if src:                                           # the Bluetooth source address HA reports, as is
                out[src] = name
    except Exception:
        pass
    return out


def wq(vals, w, q):
    o = np.argsort(vals)
    v, ww = vals[o], w[o]
    c = np.cumsum(ww)
    return float(v[int(np.searchsorted(c, q * c[-1]))])


def wmedian(vals, w):
    return wq(vals, w, 0.5)


def _hampel(v, w):
    m = wq(v, w, 0.5)
    mad = wq(np.abs(v - m), w, 0.5)
    keep = np.abs(v - m) <= 3 * 1.4826 * max(mad, 1.0)        # drop samples > 3 robust sd from the median
    return float(np.sum(v[keep] * w[keep]) / np.sum(w[keep]))


def _trim(v, w, frac=0.2):
    o = np.argsort(v)
    v, w = v[o], w[o]
    c = np.cumsum(w) / w.sum()
    keep = (c > frac) & (c - w / w.sum() < 1 - frac)
    return float(np.sum(v[keep] * w[keep]) / np.sum(w[keep])) if keep.any() else wq(v, w, 0.5)


# Per-scanner estimators, all over the same exponentially weighted window (same half-life => same lag).
ESTIMATORS = {
    "ewmed": ("EW median", wmedian),
    "hampel": ("Hampel outlier drop + EWMA", _hampel),
    "p75": ("upper quantile p75 (fades only lower RSSI)", lambda v, w: wq(v, w, 0.75)),
    "trim20": ("20% trimmed mean", _trim),
}


FULL_S = 3.0                            # full-dump interval while a tracked device is unconfigured


class _Buffers:
    def __init__(self):
        self.buf = defaultdict(deque)
        self.lock = threading.Lock()
        self.pending = defaultdict(list)          # new raw samples not yet written to the session

    def add(self, scanner, stamp, rssi):
        with self.lock:
            self.buf[scanner].append((stamp, rssi))
            self.pending[scanner].append((round(stamp, 3), rssi))

    def trim(self, now):
        with self.lock:
            for dq in self.buf.values():
                while dq and now - dq[0][0] > KEEP:
                    dq.popleft()

    def window(self, now, halflife, estimator="ewmed"):
        """{scanner: {med, n, neff, age}} using samples from the last 4 half-lives."""
        fn = ESTIMATORS.get(estimator, ESTIMATORS["ewmed"])[1]
        out = {}
        horizon = 4.0 * halflife
        with self.lock:
            items = {s: list(dq) for s, dq in self.buf.items()}
        for s, xs in items.items():
            xs = [(t, r) for t, r in xs if 0 <= now - t <= horizon and r is not None]
            if not xs:
                continue
            t = np.array([a for a, _ in xs]); r = np.array([b for _, b in xs], float)
            w = 0.5 ** ((now - t) / halflife)
            out[s] = dict(med=fn(r, w), n=len(xs), neff=round(float(w.sum() ** 2 / (w * w).sum()), 1),
                          age=round(float(now - t.max()), 1))
            if len(xs) >= 6:
                # A stationary target gives a flat window; a moving one trends. Late-half minus
                # early-half median is a cheap, robust motion signal per scanner.
                o = np.argsort(t); h = len(xs) // 2
                out[s]["trend"] = round(float(np.median(r[o[h:]]) - np.median(r[o[:h]])), 1)
                out[s]["spread"] = round(float(1.4826 * np.median(np.abs(r - np.median(r)))), 1)
        return out

    def take_pending(self):
        with self.lock:
            p, self.pending = dict(self.pending), defaultdict(list)
        return p


# The installation's fixed WAP beacons (solver/waps_v1.json where it has one - ceiling access points): positions,
# MACs and the iBeacon uuid they advertise with major = the floor. RTLS_WAPS points elsewhere; none, none wanted.
WAPS_FILE = os.environ.get("RTLS_WAPS", os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "solver", "waps_v1.json"))


def _waps(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def wap_keys(path):
    """The WAP beacons' iBeacon keys (uuid_major_1, major from the "(major N)" label), in the file's order."""
    W = _waps(path)
    uuid = W.get("_uuid")
    if not uuid:
        return []
    return [f"{uuid}_{int(lbl.split('major')[1].strip(' )'))}_1" for lbl in W
            if not lbl.startswith("_") and "major" in lbl]


class HASource:
    kind = "live"

    ANCHORS = wap_keys(WAPS_FILE)                  # the fixed WAP beacons: always wanted, the running calibration's

    def __init__(self, url, token, device, scanners, poll_s=1.0, deep_s=20.0, log=print):
        self.anchor_buf = {}               # (anchor_key, scanner) -> deque of (stamp, rssi)
        self.anchor_seen = {}
        self.anchor_seed = {}              # (anchor_key, scanner) -> reference median from the last snapshot
        try:
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            fresh = os.environ.get("RP_FRESH")       # a fresh fit's own WAP readings (solver/fresh_data.py)
            snap = json.load(open(os.path.join(os.path.join(root, fresh) if fresh else os.path.join(root, "survey"),
                                               "refsnap_waps.json")))
            W = _waps(WAPS_FILE)
            key_of = {}
            for lbl, mac in (W.get("_mac") or {}).items():
                major = int(lbl.split("major")[1].strip(" )"))
                key_of[mac.lower()] = f"{W['_uuid']}_{major}_1"
            for mac, per in snap.get("data", {}).items():
                key = key_of.get(mac.lower())
                if key:
                    for s, v in per.items():
                        self.anchor_seed[(key, s)] = float(v["med"])
        except Exception:
            pass
        self.url = url.rstrip("/") + "/api/services/bermuda/dump_devices?return_response"
        self.token, self.device, self.scanners = token, device, list(scanners)
        self.addr_name = load_addr_names()  # address -> scanner name; grows from Bermuda's own entries
        self.poll_s, self.deep_s, self.log = poll_s, deep_s, log
        # Tracked devices: one sample buffer and Bermuda info each. `device` is the focus (always
        # tracked). Both maps are replaced whole on change, so the poller never iterates a dict that
        # the web thread is mutating.
        self.devices = [device] if device else []
        self.bufs = {device: _Buffers()} if device else {}
        self.device_infos = {}
        self.full_keys = set()              # tracked keys Bermuda has not configured: full dump only
        self.last_stamp = {}                # (device key, advert key) -> newest stamp ingested
        self.offset = None                 # HA monotonic clock - wall clock
        self.present = set()               # scanners present in the configured dump
        self.deep_age = {}                 # scanner -> newest advert age from the full dump
        self.deep_at = 0.0
        self.error, self.last_ok = None, 0.0
        self.polls = 0
        self._stop = False
        self.configured_keys = set()       # device keys in the last configured-device dump
        self.rec = None                    # active calibration recording (start_recording)
        self.rec_lock = threading.Lock()

    # -- HA -------------------------------------------------------------------------------
    def _dump(self, configured, timeout=30):
        body = json.dumps({"configured_devices": configured, "redact": False}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Authorization": "Bearer " + self.token,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())["service_response"]

    @property
    def b(self):
        """The focus device's buffer."""
        return self.bufs.get(self.device) or _Buffers()

    @property
    def full_mode(self):
        return bool(self.full_keys)

    @property
    def device_info(self):
        return self.device_infos.get(self.device, {})

    def set_devices(self, keys):
        """Track these device keys (the focus device always stays tracked). Buffers of devices kept in
        the set survive; dropped ones go."""
        keys = list(dict.fromkeys(([self.device] if self.device else []) + [k for k in keys if k]))
        self.bufs = {k: self.bufs.get(k) or _Buffers() for k in keys}
        self.device_infos = {k: v for k, v in self.device_infos.items() if k in self.bufs}
        self.full_keys = self.full_keys & set(keys)
        self.devices = keys

    def set_device(self, device):
        """Change the focus device, adding it to the tracked set if needed."""
        self.device = device
        if device not in self.bufs:
            self.set_devices(self.devices)

    def take_pending(self):
        """Raw samples not yet written to the session: the focus device's (the others are drained so
        their pending lists cannot grow without bound)."""
        return self.take_pending_all().get(self.device, {})

    def take_pending_all(self):
        """Every tracked device's raw samples not yet written to the session, {key: {scanner: [(stamp, rssi)]}}:
        what the replay evaluator re-runs the live loop from (running calibration, spec 2026-09-27)."""
        out = {}
        for k, b in list(self.bufs.items()):
            p = b.take_pending()
            if p:
                out[k] = p
        return out

    def _sname(self, a):
        """Scanner name for an advert: by scanner_address first, Bermuda's label second."""
        return self.addr_name.get((a.get("scanner_address") or "").lower()) or scanner_name(a.get("name"))

    def _ename(self, v):
        """Scanner name for one of Bermuda's scanner entries."""
        for k in ("address", "address_ble_mac", "address_wifi_mac"):
            n = self.addr_name.get((v.get(k) or "").lower())
            if n:
                return n
        return scanner_name(v.get("name"))

    def _learn_addrs(self, d):
        """Whenever Bermuda's label for a scanner entry resolves to a configured scanner, remember its
        addresses, so a later relabel (HA restart) cannot lose a scanner we have no MAC on file for."""
        for v in d.values():
            if not v.get("_is_scanner"):
                continue
            n = scanner_name(v.get("name"))
            if n in self.scanners:
                for k in ("address", "address_ble_mac", "address_wifi_mac"):
                    if v.get(k):
                        self.addr_name.setdefault(v[k].lower(), n)

    def _ingest(self, d, keys=None):
        """Scanner presence, anchors and the recorder from any dump; adverts of the tracked devices
        (or just `keys`) into their buffers. True if any of those devices was in the dump."""
        seen = [v.get("last_seen") for v in d.values() if isinstance(v.get("last_seen"), (int, float))]
        if seen:
            mono = max(seen)
            off = mono - time.time()
            self.offset = off if self.offset is None else 0.8 * self.offset + 0.2 * off
        self._learn_addrs(d)
        self.present = {self._ename(v) for v in d.values() if v.get("_is_scanner")} or \
                       {self._ename(v) for v in d.values() if self._ename(v) in self.scanners}
        self._ingest_anchors(d)            # no-op until the beacons are configured devices
        self._record(d)
        bufs, found = self.bufs, False
        for key in (self.devices if keys is None else keys):
            v, buf = d.get(key), bufs.get(key)
            if v is None or buf is None:
                continue
            found = True
            self.device_infos[key] = dict(name=v.get("name"), bermuda_area=v.get("area_name"),
                                          bermuda_floor=v.get("floor_name"), last_seen=v.get("last_seen"))
            for ak, a in (v.get("adverts") or {}).items():
                st, rs = a.get("stamp"), a.get("rssi")
                if st is None or rs is None:
                    continue
                if st > self.last_stamp.get((key, ak), -1):
                    self.last_stamp[(key, ak)] = st
                    buf.add(self._sname(a), float(st), float(rs))
        return found

    def _deep_health(self):
        d = self._dump(False, timeout=60)
        now = max((v.get("last_seen") or 0) for v in d.values())
        self._learn_addrs(d)
        newest = {}
        for v in d.values():
            for a in (v.get("adverts") or {}).values():
                s = self._sname(a)
                if a.get("stamp"):
                    newest[s] = max(newest.get(s, 0), a["stamp"])
        self.deep_age = {s: (round(now - newest[s], 1) if s in newest else None) for s in self.scanners}
        self._ingest_anchors(d)
        self._record(d)
        if self.full_keys:
            self._ingest(d, keys=sorted(self.full_keys))

    def _ingest_anchors(self, d):
        """Harvest WAP-anchor adverts from ANY dump. The 20 s full dump always carries them; once
        the beacons are configured in Bermuda the 1 s configured dump carries them too, and the
        differential correction tightens from ~5 min to ~1.5 min response."""
        for key in self.ANCHORS:
            v = d.get(key)
            if not v:
                continue
            for ak, a in (v.get("adverts") or {}).items():
                st, rs = a.get("stamp"), a.get("rssi")
                if st is None or rs is None or st <= self.anchor_seen.get(ak, -1):
                    continue
                self.anchor_seen[ak] = st
                dq = self.anchor_buf.setdefault((key, self._sname(a)), deque(maxlen=4000))
                dq.append((float(st), float(rs)))

    # -- calibration recorder (app/rtls/calib.py) ------------------------------------------
    def start_recording(self, keys):
        """Record every new advert from these device keys, from both the 1 s configured poll and the
        20 s full dump, until stop_recording(). Scanners are named by address."""
        with self.rec_lock:
            self.rec = dict(keys=list(keys), samples={k: {} for k in keys}, seen={}, polls=[], t0=time.time())

    def _record(self, d):
        with self.rec_lock:
            rec = self.rec
            if rec is None:
                return
            for key in rec["keys"]:
                v = d.get(key)
                if not v:
                    continue
                for ak, a in (v.get("adverts") or {}).items():
                    st, rs = a.get("stamp"), a.get("rssi")
                    if st is None or rs is None or st <= rec["seen"].get((key, ak), -1):
                        continue
                    rec["seen"][(key, ak)] = st
                    rec["samples"][key].setdefault(self._sname(a), []).append([round(float(st), 3), float(rs)])

    def recording_counts(self):
        with self.rec_lock:
            if self.rec is None:
                return {}
            return {k: sum(len(v) for v in per.values()) for k, per in self.rec["samples"].items()}

    def stop_recording(self):
        """{samples: {key: {scanner: [[stamp, rssi], ...]}}, polls: [wall times of good polls], t0, t1}"""
        with self.rec_lock:
            rec, self.rec = self.rec, None
        if rec is None:
            return dict(samples={}, polls=[], t0=time.time(), t1=time.time())
        return dict(samples=rec["samples"], polls=rec["polls"], t0=rec["t0"], t1=time.time())

    def anchor_stats(self, n_recent=12, ref_hours=12.0):
        """Per (anchor, scanner): median of the last n_recent samples vs the reference.
        {anchor_key: {scanner: {now, now_n, span_s, ref, ref_n}}}.

        The reference is FIXED: the anchor snapshot the fit was made against (survey/refsnap_waps.json,
        ref_n = -1). A rolling 12 h median (the first version) silently absorbs a slow drift - the
        an Echo Show slid -59 -> -69 dB over a day and the correction stayed at 0.0 while a
        tag flipped rooms. Rolling is kept only as a fallback for links the snapshot lacks
        (RTLS_ANCHOR_REF=rolling restores the old behaviour)."""
        now = self.now()
        out = {}
        mode = os.environ.get("RTLS_ANCHOR_REF", "seed")
        for (key, s), dq in list(self.anchor_buf.items()):
            if not dq:
                continue
            items = list(dq)
            recent = items[-n_recent:]
            ref_items = [r for t, r in items if now is None or now - t <= ref_hours * 3600.0]
            if mode == "seed" and (key, s) in self.anchor_seed:
                ref, ref_n = self.anchor_seed[(key, s)], -1          # fixed: the fit-epoch snapshot
            elif len(ref_items) >= 20:
                ref, ref_n = float(np.median(ref_items)), len(ref_items)
            elif (key, s) in self.anchor_seed:
                ref, ref_n = self.anchor_seed[(key, s)], -1
            else:
                continue
            out.setdefault(key, {})[s] = dict(now=float(np.median([r for _, r in recent])), now_n=len(recent),
                                              span_s=round(recent[-1][0] - recent[0][0], 0),
                                              ref=ref, ref_n=ref_n)
        return out

    def anchor_window(self, horizon_s=900.0):
        """{anchor_key: {scanner: {med, n}}} over the last horizon_s of anchor adverts."""
        now = self.now()
        if now is None:
            return {}
        out = {}
        for (key, s), dq in list(self.anchor_buf.items()):
            v = [r for t, r in dq if now - t <= horizon_s]
            if v:
                out.setdefault(key, {})[s] = dict(med=float(np.median(v)), n=len(v))
        return out

    def _poll_once(self):
        try:
            d = self._dump(True)
            self.configured_keys = set(d)
            self._ingest(d)
            missing = {k for k in self.devices if k not in d}
            if missing - self.full_keys:
                self.log(f"not configured in Bermuda, polling the full dump for: {sorted(missing - self.full_keys)}")
            self.full_keys = missing
            if time.time() - self.deep_at > (FULL_S if self.full_keys else self.deep_s):
                self.deep_at = time.time()
                self._deep_health()
            self.polls += 1
            self.error, self.last_ok = None, time.time()
            with self.rec_lock:
                if self.rec is not None:
                    self.rec["polls"].append(self.last_ok)
        except Exception as e:                      # keep running through HA restarts
            self.error = f"{type(e).__name__}: {e}"[:200]

    def run(self):
        while not self._stop:
            t0 = time.time()
            self._poll_once()
            now = self.now()
            if now:
                for b in list(self.bufs.values()):
                    b.trim(now)
            dt = self.poll_s - (time.time() - t0)
            time.sleep(max(0.05, dt))

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    # -- interface -----------------------------------------------------------------------
    def now(self):
        return None if self.offset is None else time.time() + self.offset

    def window(self, halflife, estimator="ewmed", device=None):
        now, buf = self.now(), self.bufs.get(device or self.device)
        return {} if (now is None or buf is None) else buf.window(now, halflife, estimator)

    def windows(self, halflife, estimator="ewmed"):
        """{device key: window} for every tracked device."""
        now = self.now()
        return {} if now is None else {k: b.window(now, halflife, estimator) for k, b in list(self.bufs.items())}

    def snapshot(self, seconds=KEEP, now=None):
        """Snapshot now (spec 4.3): {device key: {scanner: {med, n}}}, the plain median of the samples buffered in
        the last `seconds`, for every tracked device and WAP anchor. Tracked buffers hold KEEP seconds."""
        now = self.now() if now is None else now
        if now is None:
            return {}
        rows = {}
        for key, b in list(self.bufs.items()):
            with b.lock:
                items = {s: list(dq) for s, dq in b.buf.items()}
            for s, xs in items.items():
                rows.setdefault(key, {})[s] = [r for t, r in xs if 0 <= now - t <= seconds and r is not None]
        for (key, s), dq in list(self.anchor_buf.items()):
            rows.setdefault(key, {})[s] = [r for t, r in list(dq) if 0 <= now - t <= seconds and r is not None]
        return {k: {s: dict(med=float(np.median(v)), n=len(v)) for s, v in per.items() if v}
                for k, per in rows.items() if any(per.values())}

    def dead(self):
        d = set()
        for s in self.scanners:
            if self.present and s not in self.present:
                d.add(s)
            age = self.deep_age.get(s, 0.0)
            if self.deep_age and (age is None or age > DEAD_AFTER):
                d.add(s)
        return sorted(d)

    def device_age(self, device=None):
        now, ls = self.now(), self.device_infos.get(device or self.device, {}).get("last_seen")
        return None if (now is None or ls is None) else round(now - ls, 1)

    def status(self):
        return dict(kind=self.kind, device=self.device, tracked=list(self.devices), full_mode=self.full_mode,
                    full_keys=sorted(self.full_keys), polls=self.polls,
                    error=self.error, since_ok=round(time.time() - self.last_ok, 1) if self.last_ok else None,
                    present=sorted(self.present), deep_age=self.deep_age, device_info=self.device_info)


def mono_to_wall(stamp, sent_wall, sent_mono, now_wall):
    """A bridge stamp (Home Assistant's monotonic clock) on our wall clock. The batch says what both of HA's
    clocks read when it was sent, so the two machines' clocks never need to agree. Clamped to now: a stamp
    can never land in the future."""
    return min(sent_wall - (sent_mono - stamp), now_wall)


class IngestSource(HASource):
    """Sightings pushed by the RTLS@Home bridge (POST /api/ingest, protocol v1) instead of polled from Bermuda.

    A subclass of the Bermuda source, so buffers, windows, dead-scanner logic, anchors, the calibration recorder
    and multi-device tracking are the same code. Stamps are converted to wall time on arrival, so now() is the
    wall clock and nothing polls.
    """
    kind = "ingest"

    def __init__(self, device, scanners, log=print):
        super().__init__("http://bridge.invalid", "", device, scanners, log=log)
        self.census, self.census_at = [], None
        self.unplaced = {}                 # scanner address -> HA name: heard, but not a placed scanner
        self.extra_wanted = set()          # keys the server also wants (e.g. every calibration tag)
        self.batches = 0

    def start(self):
        pass                               # fed by push(); nothing to poll

    def now(self):
        return time.time()

    def wanted(self):
        """Device keys the bridge should send sightings for."""
        with self.rec_lock:
            rec = set(self.rec["keys"]) if self.rec else set()
        return sorted(set(self.devices) | set(self.ANCHORS) | rec | set(self.extra_wanted))

    def _scanner_for(self, addr, ha_name):
        """Configured scanner name for a bridge scanner address; learns address-less scanners (the Echo Shows)
        from HA's name for them, as Bermuda's labels did."""
        name = self.addr_name.get(addr)
        if name is None:
            n = scanner_name(ha_name)
            if n in self.scanners:
                self.addr_name[addr] = name = n
        return name

    def scanner_name(self, addr):
        """Configured scanner name for a bridge scanner address, if the engine has placed that scanner."""
        return self.addr_name.get(str(addr).lower())

    def push(self, batch, now=None):
        """Ingest one bridge batch. Raises ValueError (or KeyError/TypeError/IndexError) on a malformed batch."""
        if not isinstance(batch, dict) or batch.get("v") != 1:
            raise ValueError("unsupported protocol version")
        now = time.time() if now is None else float(now)
        sent_wall, sent_mono = float(batch["sent_wall"]), float(batch["sent_mono"])
        present, ages = set(), {}
        for row in batch.get("scanners") or []:
            addr, ha_name, age = str(row[0]).lower(), str(row[1]), float(row[2])
            name = self._scanner_for(addr, ha_name)
            if name is None:
                self.unplaced[addr] = ha_name
                continue
            present.add(name)
            ages[name] = round(age, 1)
        if batch.get("scanners") is not None:
            self.present = present
            self.deep_age = {s: ages.get(s) for s in self.scanners}
        bufs, anchors = self.bufs, set(self.ANCHORS)
        for row in batch.get("adverts") or []:
            key, addr, rssi, stamp = str(row[0]), str(row[1]).lower(), float(row[2]), float(row[3])
            name = self.addr_name.get(addr)
            if name is None:
                continue
            if stamp <= self.last_stamp.get((key, addr), float("-inf")):
                continue
            self.last_stamp[(key, addr)] = stamp
            wall = mono_to_wall(stamp, sent_wall, sent_mono, now)
            buf = bufs.get(key)
            if buf is not None:
                buf.add(name, wall, rssi)
                info = self.device_infos.get(key) or {}
                self.device_infos[key] = dict(info, last_seen=max(wall, info.get("last_seen") or float("-inf")))
            if key in anchors:
                self.anchor_buf.setdefault((key, name), deque(maxlen=4000)).append((wall, rssi))
            with self.rec_lock:
                if self.rec is not None and key in self.rec["samples"]:
                    self.rec["samples"][key].setdefault(name, []).append([round(wall, 3), rssi])
        if batch.get("census") is not None:
            self.census, self.census_at = list(batch["census"]), now
            self.configured_keys = {k for d in self.census for k in d.get("keys", [])}
        with self.rec_lock:
            if self.rec is not None:
                self.rec["polls"].append(now)
        for b in list(self.bufs.values()):
            b.trim(now)
        self.batches += 1
        self.polls += 1
        self.error, self.last_ok = None, now

    def status(self):
        st = super().status()
        st.update(batches=self.batches, unplaced=self.unplaced, census=len(self.census))
        return st


def _recent(src, key, horizon_s):
    """{scanner: (n, median)} of one device's samples over the last horizon_s in a source's buffer."""
    buf = src.bufs.get(key)
    if buf is None:
        return {}
    now = src.now()
    with buf.lock:
        items = {s: [r for t, r in dq if now - t <= horizon_s] for s, dq in buf.buf.items()}
    return {s: (len(v), float(np.median(v))) for s, v in items.items() if v}


def compare_sources(a, b, keys, horizon_s=60.0):
    """Per (device, scanner): sample count and median RSSI over the last horizon_s in two sources. For the
    shadow run: a = Bermuda, b = the bridge."""
    rows = []
    for key in keys:
        ra, rb = _recent(a, key, horizon_s), _recent(b, key, horizon_s)
        for s in sorted(set(ra) | set(rb)):
            (an, am), (bn, bm) = ra.get(s, (0, None)), rb.get(s, (0, None))
            rows.append(dict(device=key, scanner=s, a_n=an, a_med=am, b_n=bn, b_med=bm))
    return rows


class ReplaySource:
    """Plays a recorded capture (solver rawcap format) as if live, optionally faster, looping."""
    kind = "replay"

    def __init__(self, path, scanners, speed=1.0, loop=True, log=print):
        d = json.load(open(path))
        self.path, self.speed, self.loop, self.scanners = path, speed, loop, list(scanners)
        self.series = {s: sorted((float(t), float(r)) for t, r in pts if r is not None)
                       for s, pts in d["raw"].items()}
        allt = [t for pts in self.series.values() for t, _ in pts]
        self.t0, self.t1 = min(allt), max(allt)
        self.wall0 = time.time()
        h = (d.get("health") or {}).get("end") or {}
        self._dead = sorted(s for s, a in h.items() if a is None or a > DEAD_AFTER)
        self.device = "replay:" + path.replace("\\", "/").split("/")[-1]
        self.b = _Buffers()
        self._cursor = {s: 0 for s in self.series}
        self._lock = threading.Lock()                # now() advances buffers; web + engine threads call it
        log(f"replaying {self.device}: {self.t1 - self.t0:.0f}s at {speed}x, dead={self._dead}")

    def start(self):
        pass

    def set_device(self, device):
        pass

    def set_devices(self, keys):
        pass                               # a replay file carries only its one recorded device

    def windows(self, halflife, estimator="ewmed"):
        return {self.device: self.window(halflife, estimator)}

    def take_pending(self):
        return self.b.take_pending()

    def take_pending_all(self):
        p = self.b.take_pending()
        return {self.device: p} if p else {}

    def now(self):
        with self._lock:
            return self._advance()

    def _advance(self):
        el = (time.time() - self.wall0) * self.speed
        dur = max(self.t1 - self.t0, 1.0)
        if self.loop and el > dur:
            self.wall0, el = time.time(), 0.0
            self._cursor = {s: 0 for s in self.series}
            with self.b.lock:
                self.b.buf.clear()
        now = self.t0 + el
        for s, pts in self.series.items():
            i = self._cursor[s]
            while i < len(pts) and pts[i][0] <= now:
                self.b.add(s, *pts[i]); i += 1
            self._cursor[s] = i
        return now

    def window(self, halflife, estimator="ewmed", device=None):
        return self.b.window(self.now(), halflife, estimator)

    configured_keys = set()                # replay has no Bermuda configuration

    def start_recording(self, keys):       # replay files carry only the one recorded device
        self._rec = dict(samples={k: {} for k in keys}, t0=time.time())

    def recording_counts(self):
        return {k: 0 for k in getattr(self, "_rec", {}).get("samples", {})}

    def stop_recording(self):
        rec = getattr(self, "_rec", None) or dict(samples={}, t0=time.time())
        self._rec = None
        return dict(samples=rec["samples"], polls=[], t0=rec["t0"], t1=time.time())

    def anchor_window(self, horizon_s=900.0):
        return {}                          # replay files carry no anchor adverts

    def anchor_stats(self, n_recent=12, ref_hours=12.0):
        return {}

    def dead(self):
        return self._dead

    def device_age(self, device=None):
        return 0.0

    def status(self):
        return dict(kind=self.kind, device=self.device, speed=self.speed,
                    position_s=round(self.now() - self.t0, 1), duration_s=round(self.t1 - self.t0, 1),
                    error=None, dead_from_file=self._dead)
