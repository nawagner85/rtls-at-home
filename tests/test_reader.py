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
    def __init__(self, rssi, manufacturer_data=None, local_name=None, service_uuids=None, service_data=None,
                 tx_power=None):
        self.rssi, self.manufacturer_data, self.local_name = rssi, manufacturer_data or {}, local_name
        self.service_uuids, self.service_data, self.tx_power = service_uuids or [], service_data or {}, tx_power


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


def test_census_rows_carry_what_identifies_a_device():
    """Manufacturer ids with their first bytes, service ids (16-bit when standard) with their first data bytes, and
    the advertised transmit power: the engine names the maker and kind from them during onboarding."""
    base = "-0000-1000-8000-00805f9b34fb"
    adv = Adv(-60, {0x004C: bytes.fromhex("1219100000aabbccdd")},
              service_uuids=["0000fe2c" + base, "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"],
              service_data={"0000fe2c" + base: bytes.fromhex("0a1b2c")}, tx_power=-8)
    sc = Scanner("00:00:5E:00:53:F1", "p1", {MAC_A: (-60, 99.0, None, None), MAC_B: (-70, 99.0, None, None)})
    sc.discovered_devices_and_advertisement_data[MAC_A] = (Dev(MAC_A), adv)
    sc.discovered_devices_and_advertisement_data[MAC_B] = (Dev(MAC_B), Adv(-70, tx_power=-127))
    a, b = census([sc], now_mono=100.0, window=60.0, cap=10)
    assert a["mfr"] == [[76, "1219100000aa"]] and a["tx"] == -8
    assert a["svc"] == ["6e400001-b5a3-f393-e0a9-e50e24dcca9e", "fe2c"] and a["sdata"] == {"fe2c": "0a1b2c"}
    assert not {"mfr", "svc", "sdata", "tx"} & set(b)          # nothing to say: nothing sent (-127 = not given)
