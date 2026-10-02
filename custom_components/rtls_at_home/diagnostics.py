"""Download diagnostics: what the bridge and the engine are doing, for a bug report - which may be public, so the
ingest token, the receivers' addresses and the tracked devices' keys (phone and tag addresses) are left out."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import RtlsConfigEntry
from .const import CONF_TOKEN

TO_REDACT = {CONF_TOKEN}
META_REDACT = {"mac", "address"}


def _short(key: str) -> str:
    """A tracked device's key (a Bluetooth address or iBeacon identity) as a stable, anonymous label."""
    return "device-" + hashlib.sha256(key.encode()).hexdigest()[:8]
ROW_FIELDS = ("name", "status", "room", "floor", "area", "verdict", "p_room", "age")


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: RtlsConfigEntry) -> dict[str, Any]:
    runner = entry.runtime_data
    now = time.monotonic()
    return {
        "entry": {"data": async_redact_data(dict(entry.data), TO_REDACT), "options": dict(entry.options)},
        "runner": {
            "available": runner.available,
            "last_ok_age": round(now - runner.last_ok, 1) if runner.last_ok is not None else None,
            "backoff": runner._backoff,
            "poll": runner.poll,
            "census_every": runner.census_every,
            "wanted": len(runner.wanted),
            "detail": runner.detail is not None,
        },
        "tracked": {_short(key): {f: row.get(f) for f in ROW_FIELDS} for key, row in runner.tracked.items()},
        "meta": async_redact_data(runner.meta, META_REDACT) if runner.meta else None,
    }
