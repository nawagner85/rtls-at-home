"""Present or away, and where each tracked device was last seen (spec 3.2).

Runtime state, not configuration: saved to device_state.json on the sessions volume on every present/away change
and at most once a minute otherwise, so a restart still knows where a quiet device was last seen.
"""
import json
import os
import time

from calib import write_json_atomic

AWAY_AFTER = float(os.environ.get("RTLS_AWAY_AFTER", "120"))   # s without an advert; tests shorten it
SAVE_EVERY = 60.0
_EMPTY = dict(status=None, last_seen=None, last_room=None, last_floor=None)


class Presence:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = path, clock
        self._saved_at = float("-inf")
        try:
            with open(path, encoding="utf-8") as f:
                rows = json.load(f).get("devices") or {}
            self.rows = {str(k): {f: v.get(f) for f in _EMPTY} for k, v in rows.items() if isinstance(v, dict)}
        except (OSError, ValueError, AttributeError):
            self.rows = {}
        started = clock()
        # The grace period's start: process start for saved devices, first update for devices tracked later.
        self._since = {k: started for k in self.rows}

    def row(self, key):
        return dict(self.rows.get(key) or _EMPTY)

    def status(self, key):
        return (self.rows.get(key) or _EMPTY)["status"]

    def update(self, key, age):
        """One tick. age: seconds since the device's last advert, None if not heard since the engine started.
        Returns "returned" (away -> present), "left" (present -> away) or None."""
        now = self.clock()
        r = self.rows.setdefault(key, dict(_EMPTY))
        since = self._since.setdefault(key, now)
        if age is not None:
            r["last_seen"] = round(now - age, 1)
            new = "present" if age < AWAY_AFTER else "away"
        elif now - since < AWAY_AFTER:
            # Not heard since this process started (or since tracking began): the bridge may not have reported yet.
            # Keep what was saved - or present for a new device - instead of flipping HA to not_home and back.
            new = r["status"] or "present"
        else:
            new = "away"
        old, r["status"] = r["status"], new
        self._save(now, force=old != new)
        return {("away", "present"): "returned", ("present", "away"): "left"}.get((old, new))

    def note(self, key, room, floor):
        """The room and floor of the device's latest estimate."""
        r = self.rows.setdefault(key, dict(_EMPTY))
        if room and (r["last_room"], r["last_floor"]) != (room, floor):
            r["last_room"], r["last_floor"] = room, floor
            self._save(self.clock())

    def forget(self, key):
        if self.rows.pop(key, None) is not None:
            self._save(self.clock(), force=True)

    def _save(self, now, force=False):
        if force or now - self._saved_at >= SAVE_EVERY:
            self._saved_at = now
            try:
                write_json_atomic(self.path, dict(version=1, devices=self.rows))
            except OSError:
                pass                     # the next save retries; tracking never stops over this
