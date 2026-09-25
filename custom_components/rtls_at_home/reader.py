"""Turn Home Assistant's Bluetooth scanner state into protocol-v1 sightings.

Home Assistant keeps, per scanner, the latest advertisement from each device and the time it arrived
(habluetooth `BaseHaScanner.discovered_devices_and_advertisement_data` and `discovered_device_timestamps`,
monotonic seconds). Polling that faster than a scanner refreshes it captures every update exactly once.
Pure Python: no Home Assistant imports, so it is easy to test.
"""

from __future__ import annotations

import statistics
from collections import deque
from typing import Any

from .const import DETAIL_MAX, DETAIL_WINDOW

APPLE = 0x004C


def ibeacon_key(manufacturer_data: dict[int, bytes]) -> str | None:
    """`<uuid>_<major>_<minor>` for an Apple iBeacon payload (type 0x02, length 0x15), else None."""
    data = manufacturer_data.get(APPLE)
    if not data or len(data) < 22 or data[0] != 0x02 or data[1] != 0x15:
        return None
    uuid = data[2:18].hex()
    major = int.from_bytes(data[18:20], "big")
    minor = int.from_bytes(data[20:22], "big")
    return f"{uuid}_{major}_{minor}"


def device_keys(address: str, adv: Any) -> list[str]:
    """Every key a sighting is known by: the lower-case address, plus its iBeacon identity if it has one."""
    keys = [address.lower()]
    beacon = ibeacon_key(getattr(adv, "manufacturer_data", None) or {})
    if beacon:
        keys.append(beacon)
    return keys


def address_type(address: str, details: Any) -> str | None:
    """What kind of Bluetooth address this is, from the advertisement details (ESPHome proxies report address_type
    0 = public, 1 = random). A random address's top two bits say which kind: 11 static, 01 resolvable (rotating,
    needs an identity key), 00 non-resolvable. None when the scanner doesn't say."""
    kind = details.get("address_type") if isinstance(details, dict) else None
    if kind == 0:
        return "public"
    if kind != 1:
        return None
    return {3: "random_static", 1: "random_resolvable", 0: "random_nonresolvable"}.get(int(address[:2], 16) >> 6)


class CensusDetail:
    """Each proxy's recent readings of devices the engine doesn't track yet, for onboarding (spec 3.4)."""

    def __init__(self, window: float = DETAIL_WINDOW, cap: int = DETAIL_MAX) -> None:
        self.window, self.cap = window, cap
        self._by: dict[str, dict[str, deque]] = {}  # address -> scanner -> (stamp, rssi)
        self._type: dict[str, str | None] = {}

    @property
    def size(self) -> int:
        return len(self._by)

    def add(self, address: str, source: str, rssi: int, stamp: float, addr_type: str | None) -> None:
        by = self._by.get(address)
        if by is None:
            if len(self._by) >= self.cap:
                return
            by = self._by[address] = {}
        by.setdefault(source, deque(maxlen=64)).append((stamp, rssi))
        if addr_type is not None:
            self._type[address] = addr_type

    def annotate(self, rows: list[dict], now_mono: float, first_seen: dict[str, float], to_wall: Any) -> None:
        """Add per_scanner, first_seen and addr_type to census rows in place, after dropping readings older than
        the window. to_wall converts a monotonic stamp to wall-clock seconds."""
        cutoff = now_mono - self.window
        for address in list(self._by):
            by = self._by[address]
            for source in list(by):
                dq = by[source]
                while dq and dq[0][0] < cutoff:
                    dq.popleft()
                if not dq:
                    del by[source]
            if not by:
                del self._by[address]
                self._type.pop(address, None)
        for row in rows:
            address = row["keys"][0]
            by = self._by.get(address) or {}
            row["per_scanner"] = [[source, statistics.median(r for _, r in dq), len(dq)]
                                  for source, dq in sorted(by.items())]
            stamp = first_seen.get(address)
            row["first_seen"] = round(to_wall(stamp), 1) if stamp is not None else None
            row["addr_type"] = self._type.get(address)


class SightingReader:
    """Reads scanners, sending each (scanner, address, stamp) at most once."""

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], float] = {}
        self.first_seen: dict[str, float] = {}  # lower-case address -> first stamp

    @property
    def size(self) -> int:
        return len(self._last)

    def read(self, scanners: list[Any], wanted: set[str],
             detail: CensusDetail | None = None) -> tuple[list[list], list[list]]:
        """(sightings, scanner health). Sightings are `[key, scanner, rssi, stamp]` for wanted keys only; readings
        of everything else go to `detail` when onboarding asked for it. Health is `[scanner, name, seconds since
        its last advertisement]` for every scanner."""
        sightings: list[list] = []
        health: list[list] = []
        for scanner in scanners:
            source = scanner.source.lower()
            health.append([source, scanner.name, round(float(scanner.time_since_last_detection()), 2)])
            stamps = scanner.discovered_device_timestamps
            for address, (device, adv) in scanner.discovered_devices_and_advertisement_data.items():
                stamp = stamps.get(address)
                if stamp is None:
                    continue
                seen = (source, address)
                if self._last.get(seen, float("-inf")) >= stamp:
                    continue
                self._last[seen] = stamp
                lower = address.lower()
                self.first_seen.setdefault(lower, stamp)
                hit = False
                for key in device_keys(address, adv):
                    if key in wanted:
                        sightings.append([key, source, adv.rssi, round(stamp, 3)])
                        hit = True
                if detail is not None and not hit:
                    detail.add(lower, source, adv.rssi, stamp, address_type(lower, getattr(device, "details", None)))
        return sightings, health

    def prune(self, now_mono: float, keep: float) -> None:
        """Forget dedupe entries older than `keep` seconds (addresses rotate; the table must not grow forever), and
        the first-heard time of addresses no longer heard: one that comes back later counts as new again."""
        self._last = {k: st for k, st in self._last.items() if now_mono - st <= keep}
        live = {address.lower() for _, address in self._last}
        self.first_seen = {a: st for a, st in self.first_seen.items() if a in live}


def census(scanners: list[Any], now_mono: float, window: float, cap: int) -> list[dict]:
    """Every device heard in the last `window` seconds, merged across scanners, loudest first, at most `cap`."""
    best: dict[str, dict] = {}
    for scanner in scanners:
        stamps = scanner.discovered_device_timestamps
        for address, (device, adv) in scanner.discovered_devices_and_advertisement_data.items():
            stamp = stamps.get(address)
            if stamp is None or now_mono - stamp > window:
                continue
            age = round(now_mono - stamp, 1)
            row = best.get(address)
            if row is None:
                keys = device_keys(address, adv)
                best[address] = {"keys": keys, "name": device.name or getattr(adv, "local_name", None),
                                 "ibeacon": len(keys) > 1, "rssi": adv.rssi, "scanners": 1, "age": age}
            else:
                row["scanners"] += 1
                row["rssi"] = max(row["rssi"], adv.rssi)
                row["age"] = min(row["age"], age)
    return sorted(best.values(), key=lambda r: -r["rssi"])[:cap]
