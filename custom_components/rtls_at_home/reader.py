"""Turn Home Assistant's Bluetooth scanner state into protocol-v1 sightings.

Home Assistant keeps, per scanner, the latest advertisement from each device and the time it arrived
(habluetooth `BaseHaScanner.discovered_devices_and_advertisement_data` and `discovered_device_timestamps`,
monotonic seconds). Polling that faster than a scanner refreshes it captures every update exactly once.
Pure Python: no Home Assistant imports, so it is easy to test.
"""

from __future__ import annotations

from typing import Any

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


class SightingReader:
    """Reads scanners, sending each (scanner, address, stamp) at most once."""

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], float] = {}

    @property
    def size(self) -> int:
        return len(self._last)

    def read(self, scanners: list[Any], wanted: set[str]) -> tuple[list[list], list[list]]:
        """(sightings, scanner health). Sightings are `[key, scanner, rssi, stamp]` for wanted keys only;
        health is `[scanner, name, seconds since its last advertisement]` for every scanner."""
        sightings: list[list] = []
        health: list[list] = []
        for scanner in scanners:
            source = scanner.source.lower()
            health.append([source, scanner.name, round(float(scanner.time_since_last_detection()), 2)])
            stamps = scanner.discovered_device_timestamps
            for address, (_device, adv) in scanner.discovered_devices_and_advertisement_data.items():
                stamp = stamps.get(address)
                if stamp is None:
                    continue
                seen = (source, address)
                if self._last.get(seen, float("-inf")) >= stamp:
                    continue
                self._last[seen] = stamp
                for key in device_keys(address, adv):
                    if key in wanted:
                        sightings.append([key, source, adv.rssi, round(stamp, 3)])
        return sightings, health

    def prune(self, now_mono: float, keep: float) -> None:
        """Forget dedupe entries older than `keep` seconds (addresses rotate; the table must not grow forever)."""
        self._last = {k: st for k, st in self._last.items() if now_mono - st <= keep}


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
