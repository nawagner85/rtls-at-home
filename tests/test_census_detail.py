"""Onboarding's detailed census: each proxy's recent readings of devices the engine doesn't track yet."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.rtls_at_home.reader import CensusDetail, SightingReader, address_type, census

from .test_reader import MAC_A, MAC_B, Scanner
from .test_runner_sensor import KEY, URL, _setup


def _mac(first: int) -> str:
    """A documentation-range address (RFC 7042) with its first byte set: the random-address kinds live in the top
    two bits of that byte. Built at runtime so no real-looking address appears in the public tree."""
    return f"{first:02x}:00:5e:00:53:0a"


def test_address_type_from_advertisement_details() -> None:
    assert address_type(_mac(0xAA), {"address_type": 0}) == "public"
    assert address_type(_mac(0xC4), {"address_type": 1}) == "random_static"         # top bits 11
    assert address_type(_mac(0x5A), {"address_type": 1}) == "random_resolvable"     # top bits 01
    assert address_type(_mac(0x1A), {"address_type": 1}) == "random_nonresolvable"  # top bits 00
    assert address_type(_mac(0xAA), {}) is None and address_type(_mac(0xAA), None) is None


def test_read_feeds_detail_only_for_devices_not_wanted() -> None:
    sc = Scanner("00:00:5E:00:53:F1", "p", {MAC_A: (-60, 10.0, None, None), MAC_B: (-70, 10.0, None, None)})
    sc.discovered_devices_and_advertisement_data[MAC_B][0].details = {"address_type": 1}
    r, d = SightingReader(), CensusDetail()
    sightings, _ = r.read([sc], {MAC_A.lower()}, d)
    assert [s[0] for s in sightings] == [MAC_A.lower()] and d.size == 1
    rows = [{"keys": [MAC_B.lower()]}, {"keys": [MAC_A.lower()]}]
    d.annotate(rows, 10.5, r.first_seen, lambda st: 1000.0 + st)
    assert rows[0]["per_scanner"] == [["00:00:5e:00:53:f1", -70, 1]] and rows[0]["first_seen"] == 1010.0
    assert rows[0]["addr_type"] == "random_nonresolvable"          # 00:00:5E... top bits 00
    assert rows[1]["per_scanner"] == [] and rows[1]["first_seen"] == 1010.0


def test_detail_keeps_a_rolling_window_and_a_cap() -> None:
    d = CensusDetail(window=15.0, cap=2)
    for i in range(10):
        d.add("00:00:5e:00:53:01", "p1", -60 - i, 100.0 + i, None)
    d.add("00:00:5e:00:53:02", "p1", -70, 100.0, None)
    d.add("00:00:5e:00:53:03", "p1", -80, 100.0, None)             # over the cap: dropped
    rows = [{"keys": [f"00:00:5e:00:53:0{i}"]} for i in (1, 2, 3)]
    d.annotate(rows, 116.0, {}, lambda st: st)
    assert rows[0]["per_scanner"] == [["p1", -65, 9]]                # samples at 101..109 (-61..-69): median -65
    assert rows[1]["per_scanner"] == [] and rows[2]["per_scanner"] == []   # 100.0 is outside; 03 never kept
    assert d.size == 1


def test_first_seen_is_kept_while_heard_and_pruned_with_the_dedupe_table() -> None:
    sc = Scanner("00:00:5E:00:53:F1", "p", {MAC_A: (-60, 10.0, None, None)})
    r = SightingReader()
    r.read([sc], set())
    sc.discovered_device_timestamps[MAC_A] = 50.0
    r.read([sc], set())
    assert r.first_seen == {MAC_A.lower(): 10.0}
    r.prune(400.0, 300.0)
    assert r.first_seen == {}


def test_full_detailed_census_fits_in_a_batch() -> None:
    seen = {f"00:00:5E:00:{i // 256:02X}:{i % 256:02X}": (-70, 10.0, None, "unnamed device") for i in range(500)}
    scanners = [Scanner(f"00:00:5E:00:53:{j:02X}", f"proxy-{j}", seen) for j in range(9)]
    r, d = SightingReader(), CensusDetail()
    r.read(scanners, set(), d)
    rows = census(scanners, 11.0, 60.0, 500)
    d.annotate(rows, 11.0, r.first_seen, lambda st: time.time())
    body = json.dumps({"v": 1, "census": rows})
    assert len(rows) == 500 and all(len(x["per_scanner"]) == 9 for x in rows)
    assert len(body) < 512 * 1024                                     # about 230 KB; the engine accepts 1 MB


async def test_runner_sends_detail_only_while_the_engine_asks(hass: HomeAssistant, aioclient_mock) -> None:
    now = time.monotonic()                     # the detail window is real time: stamps must be recent
    sc = Scanner("00:00:5E:00:53:F1", "proxy-1", {MAC_A: (-60, now, None, None), MAC_B: (-70, now, None, None)})
    entry = await _setup(hass, aioclient_mock, {"wanted": [KEY], "tracked": [], "census_detail": True})
    runner = entry.runtime_data
    with patch("custom_components.rtls_at_home.runner.current_scanners", return_value=[sc]):
        runner._next_try = 0.0
        await runner.tick()                    # this reply turns detail on
        assert runner.detail is not None
        for mac in (MAC_A, MAC_B):
            sc.discovered_device_timestamps[mac] = time.monotonic()
        runner._next_census, runner._next_try = 0.0, 0.0
        await runner.tick()                    # MAC_B is not wanted: its reading goes to the detailed census
    posted = aioclient_mock.mock_calls[-1][2]
    row_b = next(r for r in posted["census"] if r["keys"][0] == MAC_B.lower())
    assert row_b["per_scanner"] == [["00:00:5e:00:53:f1", -70, 1]] and row_b["first_seen"] is not None
    assert runner._next_census - time.monotonic() <= 5.5
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{URL}/api/ingest", json={"wanted": [KEY], "tracked": []})
    with patch("custom_components.rtls_at_home.runner.current_scanners", return_value=[sc]):
        runner._next_try = 0.0
        await runner.tick()                    # a reply without census_detail turns it off
        assert runner.detail is None
        runner._next_census, runner._next_try = 0.0, 0.0
        await runner.tick()
    assert all("per_scanner" not in row for row in aioclient_mock.mock_calls[-1][2]["census"])
