"""The device list: every device the engine knows, its friendly name, and what was removed on purpose.

Spec 3.1 (docs/specs/2026-09-25-onboarding-design.md). Configuration, not runtime state: it changes only when
someone edits it, and lives in devices.json on the sessions volume next to track_cfg.json.
"""
import json
import os
import re
import time
from datetime import datetime

from calib import write_json_atomic

VERSION = 1
KINDS = ("phone", "tag", "pet")     # the transmit-power prior Engine.step uses: phone 4 dB, anything else 12 dB;
                                    # phones and pets move (a 30 s room filter), tags sit (60 s) - engine.filter_tau
ROLES = ("tracked", "fixed")
TOMBSTONE_S = 7 * 86400.0
NAME_MAX = 64
_KEY = re.compile(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}|[0-9a-f]{32}_\d{1,5}_\d{1,5}|irk:[0-9a-f]{16}")
_TAG_N = re.compile(r"Tag (\d+)\b")
_FIELDS = {"name", "kind", "role", "tracked"}


class DeviceError(ValueError):
    """A bad key, name or field: HTTP 400."""


class DeviceExists(DeviceError):
    """Adding a key that is already on the list: HTTP 409."""


class NoSuchDevice(KeyError):
    """A key that is not on the list: HTTP 404."""


def check_key(key):
    k = str(key or "").strip().lower()
    if not _KEY.fullmatch(k):
        raise DeviceError(f"not a device key: {key!r} (a MAC like 00:00:5e:00:53:01, or an iBeacon uuid_major_minor)")
    return k


def check_name(name):
    n = " ".join(str(name or "").split())
    if not 1 <= len(n) <= NAME_MAX:
        raise DeviceError(f"a name needs 1-{NAME_MAX} characters")
    return n


def _iso(t):
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def _epoch(s):
    return datetime.fromisoformat(s).timestamp()


def _entry(v):
    """A validated copy of one entry. Fields this version doesn't know (phase 2 placements) are kept."""
    if not isinstance(v, dict):
        raise DeviceError("a device entry must be an object")
    out = dict(v)
    out["name"] = check_name(v.get("name"))
    out["kind"], out["role"] = v.get("kind", "tag"), v.get("role", "tracked")
    if out["kind"] not in KINDS:
        raise DeviceError(f"kind must be one of: {', '.join(KINDS)}")
    if out["role"] not in ROLES:
        raise DeviceError(f"role must be one of: {', '.join(ROLES)}")
    out["tracked"] = out["role"] == "tracked"          # a listed tracked-role device is tracked; delete is the off switch (2026-09-26)
    return out


class DeviceList:
    def __init__(self, path, seed, clock=time.time):
        """seed: {key: entry}, used when devices.json is missing or unreadable."""
        self.path, self.clock = path, clock
        self.recovered = None               # {backup, error} when an unreadable file was set aside
        self.devices, self.removed = {}, {}
        self.issued = {}                    # key -> calibration id, every one ever given (rounds are fitted by id)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict) or d.get("version") != VERSION or not isinstance(d.get("devices"), dict):
                raise ValueError(f"not a version-{VERSION} device list")
            self.devices = {check_key(k): _entry(v) for k, v in d["devices"].items()}
            self.removed = {check_key(k): str(t) for k, t in (d.get("removed") or {}).items()}
            self.issued = {check_key(k): str(v) for k, v in (d.get("issued") or {}).items()}
            for t in self.removed.values():
                _epoch(t)
        except FileNotFoundError:
            self._reseed(seed)
        except (OSError, ValueError, TypeError, AttributeError) as e:
            backup = path + ".bak"
            os.replace(path, backup)
            self.recovered = dict(backup=backup, error=f"{type(e).__name__}: {e}")
            self._reseed(seed)
        for k, v in self.devices.items():       # a list from before the record: its devices' ids
            if v.get("calib_id"):
                self.issued.setdefault(k, v["calib_id"])
        self.expire()

    def _reseed(self, seed):
        self.devices = {check_key(k): _entry(v) for k, v in seed.items()}
        self.removed, self.issued = {}, {}
        self.save()

    # ---- reading -----------------------------------------------------------------------
    def __contains__(self, key):
        return key in self.devices

    def get(self, key):
        if key not in self.devices:
            raise NoSuchDevice(key)
        return self.devices[key]

    def name(self, key, default=None):
        e = self.devices.get(key)
        return e["name"] if e else default

    def kind(self, key):
        e = self.devices.get(key)
        return e["kind"] if e else "phone"

    def tracked_keys(self):
        return [k for k, v in self.devices.items() if v["tracked"]]

    def _next_calib_id(self):
        used = set(self.issued.values()) | set(self.calib_ids().values())   # never a removed tag's (A5 review)
        n = 1
        while f"tag{n}" in used:
            n += 1
        return f"tag{n}"

    def calib_ids(self):
        return {k: v["calib_id"] for k, v in self.devices.items() if v.get("calib_id")}

    def tombstones(self):
        self.expire()
        return sorted(self.removed)

    def view(self):
        """What GET /api/devices returns."""
        return dict(devices=[dict(key=k, **v) for k, v in self.devices.items()],
                    removed=[dict(key=k, at=t) for k, t in sorted(self.removed.items())],
                    recovered=self.recovered)

    # ---- editing: every call saves ----------------------------------------------------
    def add(self, key, name, kind="tag", role="tracked"):
        key = check_key(key)
        if key in self.devices:
            raise DeviceExists(f"{key} is already on the list as {self.devices[key]['name']!r}")
        e = _entry(dict(name=name, kind=kind, role=role, tracked=role == "tracked", added=_iso(self.clock())))
        m = _TAG_N.match(e["name"])
        if key in self.issued:
            e["calib_id"] = self.issued[key]       # the same tag again: its id - and its rounds - back
        elif m and f"tag{m.group(1)}" not in set(self.issued.values()) | set(self.calib_ids().values()):
            e["calib_id"] = f"tag{m.group(1)}"     # a new "Tag N" survey tag keeps the older rounds' id, pinned now
        elif e["kind"] == "tag":
            e["calib_id"] = self._next_calib_id()  # any tag can be placed in a calibration round (A5 clean room)
        if e.get("calib_id"):
            self.issued[key] = e["calib_id"]
        self.devices[key] = e
        self.removed.pop(key, None)
        self.save()
        return e

    def update(self, key, fields):
        old = self.get(key)
        unknown = set(fields) - _FIELDS
        if unknown:
            raise DeviceError(f"unknown field(s): {', '.join(sorted(unknown))}")
        new = dict(old, **fields)
        if fields.get("role") == "tracked" and "tracked" not in fields:
            new["tracked"] = True
        new = _entry(new)
        if new["kind"] == "tag" and not new.get("calib_id"):
            new["calib_id"] = self.issued.get(key) or self._next_calib_id()
            self.issued[key] = new["calib_id"]
        self.devices[key] = new
        if old["role"] == "tracked" and new["role"] == "fixed":
            self.removed[key] = _iso(self.clock())    # fixed anchors have no HA entities
        elif new["role"] == "tracked":
            self.removed.pop(key, None)
        self.save()
        return new

    def remove(self, key):
        self.get(key)
        del self.devices[key]
        self.removed[key] = _iso(self.clock())
        self.save()

    def expire(self):
        now = self.clock()
        gone = [k for k, t in self.removed.items() if now - _epoch(t) > TOMBSTONE_S]
        for k in gone:
            del self.removed[k]
        if gone:
            self.save()

    def save(self):
        write_json_atomic(self.path, dict(version=VERSION, devices=self.devices, removed=self.removed,
                                          issued=self.issued))
