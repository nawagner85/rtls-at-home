"""Reading Home Assistant's Bluetooth scanners into protocol v1 sightings."""

from __future__ import annotations

from custom_components.rtls_at_home.reader import SightingReader, census, device_keys, ibeacon_key

UUID = "00112233445566778899aabbccddeeff"
MAC_A = "00:00:5E:00:53:0A"
MAC_B = "00:00:5E:00:53:0B"


def ibeacon(major: int, minor: int) -> dict[int, bytes]:
    payload = bytes([0x02, 0x15]) + bytes.fromhex(UUID) + major.to_bytes(2, "big") + minor.to_bytes(2, "big")
    return {0x004C: payload + b"\xc5"}


class Adv:
    def __init__(self, rssi, manufacturer_data=None, local_name=None):
        self.rssi, self.manufacturer_data, self.local_name = rssi, manufacturer_data or {}, local_name


class Dev:
    def __init__(self, address, name=None):
        self.address, self.name = address, name


class Scanner:
    def __init__(self, source, name, seen, idle=0.5):
        self.source, self.name, self._idle = source, name, idle
        self.discovered_devices_and_advertisement_data = {
            a: (Dev(a, n), Adv(r, m, n)) for a, (r, _st, m, n) in seen.items()}
        self.discovered_device_timestamps = {a: st for a, (_r, st, _m, _n) in seen.items()}

    def time_since_last_detection(self):
        return self._idle


def test_ibeacon_key_and_device_keys():
    assert ibeacon_key(ibeacon(100, 40004)) == f"{UUID}_100_40004"
    assert ibeacon_key({0x004C: b"\x10\x05"}) is None and ibeacon_key({}) is None
    assert device_keys(MAC_A, Adv(-60, ibeacon(1, 2))) == [MAC_A.lower(), f"{UUID}_1_2"]


def test_read_sends_only_new_wanted_sightings_with_lowercase_sources():
    sc = Scanner("00:00:5E:00:53:F1", "proxy-1", {MAC_A: (-60, 10.0, None, None), MAC_B: (-70, 10.0, None, None)})
    r = SightingReader()
    sightings, health = r.read([sc], {MAC_A.lower()})
    assert sightings == [[MAC_A.lower(), "00:00:5e:00:53:f1", -60, 10.0]]
    assert health == [["00:00:5e:00:53:f1", "proxy-1", 0.5]]
    assert r.read([sc], {MAC_A.lower()})[0] == []          # same stamp: not sent again
    sc.discovered_device_timestamps[MAC_A] = 10.6
    assert r.read([sc], {MAC_A.lower()})[0] == [[MAC_A.lower(), "00:00:5e:00:53:f1", -60, 10.6]]


def test_rotating_address_keeps_its_ibeacon_key():
    key = f"{UUID}_7_8"
    r = SightingReader()
    one = Scanner("00:00:5E:00:53:F1", "p", {MAC_A: (-60, 10.0, ibeacon(7, 8), None)})
    two = Scanner("00:00:5E:00:53:F1", "p", {MAC_B: (-61, 10.0, ibeacon(7, 8), None)})
    assert [s[0] for s in r.read([one], {key})[0]] == [key]
    assert [s[0] for s in r.read([two], {key})[0]] == [key]     # new address, same stamp: still new


def test_prune_bounds_the_dedupe_table():
    r = SightingReader()
    r.read([Scanner("00:00:5E:00:53:F1", "p", {MAC_A: (-60, 10.0, None, None)})], set())
    r.prune(now_mono=500.0, keep=300.0)
    assert r.size == 0


def test_census_merges_scanners_sorts_loudest_first_and_caps():
    a = Scanner("00:00:5E:00:53:F1", "p1", {MAC_A: (-80, 99.0, None, "tag"), MAC_B: (-50, 99.5, ibeacon(1, 2), None)})
    b = Scanner("00:00:5E:00:53:F2", "p2", {MAC_A: (-65, 99.8, None, "tag")})
    rows = census([a, b], now_mono=100.0, window=60.0, cap=10)
    assert [r["keys"][0] for r in rows] == [MAC_B.lower(), MAC_A.lower()]
    tag = rows[1]
    assert tag == {"keys": [MAC_A.lower()], "name": "tag", "ibeacon": False, "rssi": -65, "scanners": 2, "age": 0.2}
    assert rows[0]["ibeacon"] is True
    assert len(census([a, b], now_mono=100.0, window=60.0, cap=1)) == 1
    assert census([a, b], now_mono=500.0, window=60.0, cap=10) == []
