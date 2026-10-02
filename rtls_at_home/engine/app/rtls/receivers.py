"""Receivers switched on and off from Setup (Nick 2026-09-27). An off receiver is left out of tracking and onboarding
the way a dead one is - its readings are neither evidence nor a "didn't hear it" - so a receiver that has gone wrong (a
new board whose pattern the fit hasn't learned, one knocked over) can be taken out without a redeploy. Samples are
still recorded and calibration captures still keep them. Persisted in the sessions folder (receivers.json)."""
import json
import os


class ReceiverSwitches:
    def __init__(self, path, known):
        self.path, self.known = path, list(known)
        self.disabled = set()
        try:
            self.disabled = set(json.load(open(path, encoding="utf-8")).get("disabled") or []) & set(self.known)
        except (OSError, ValueError, AttributeError):
            self.disabled = set()

    def enabled(self, name):
        return name not in self.disabled

    def set(self, name, on):
        if name not in self.known:
            raise ValueError(f"unknown receiver {name!r}")
        (self.disabled.discard if on else self.disabled.add)(name)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(disabled=sorted(self.disabled)), f)
        os.replace(tmp, self.path)
