"""What an unknown Bluetooth device probably is, from its advertisement - for onboarding, where a name is often all a
device offers and the MAC address can't be checked on the device itself (Nick 2026-09-27).

The bridge's census rows carry, besides keys and name: addr_type (from the proxies), mfr [[company id, first bytes
hex], ...], svc [16-bit service ids], sdata {service id: first bytes hex} and tx. An older bridge sends none of the
advertisement fields; the address prefix still works then.

Names come from bt_names.json.gz (tools/build_bt_names.py: Bluetooth SIG assigned numbers and the IEEE registry).
Order of evidence, most specific first:
  1. Apple's continuity messages (manufacturer id 0x004C, not iBeacon): the message type says iPhone, AirPods, Find My
  2. iBeacon: the kind; the maker only from a PUBLIC address prefix - every iBeacon uses Apple's id, whoever made it
  3. Microsoft beacons, Google Fast Pair, standard services (keyboard, heart rate, ...): the kind
  4. the manufacturer id, then a member service id, then a public address prefix: the vendor
A random address never gets a vendor from its prefix: its first bytes only resemble a prefix by chance.
"""
import gzip
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
APPLE, MICROSOFT = 76, 6

APPLE_TYPES = {0x12: "Find My beacon (AirTag or another Find My device)", 0x07: "AirPods or Beats",
               0x10: "iPhone, iPad or Apple Watch", 0x0C: "iPhone, iPad or Mac", 0x05: "iPhone, iPad or Mac",
               0x0F: "iPhone or iPad", 0x09: "AirPlay speaker or TV"}
MICROSOFT_TYPES = {0x01: "Windows PC", 0x03: "accessory ready to pair (Swift Pair)"}
SERVICE_KINDS = {"1812": "keyboard, mouse or remote", "180d": "heart-rate sensor", "181a": "environment sensor",
                 "1809": "thermometer", "1810": "blood-pressure monitor", "1826": "fitness machine",
                 "feaa": "Eddystone beacon", "fcd2": "BTHome sensor", "fd6f": "phone (exposure notification)"}
FAST_PAIR = "fe2c"

_T = None


def table():
    global _T
    if _T is None:
        with gzip.open(os.path.join(HERE, "bt_names.json.gz"), "rt", encoding="utf-8") as f:
            _T = json.load(f)
    return _T


def _first_byte(hexs):
    try:
        return int(str(hexs)[:2], 16)
    except (TypeError, ValueError):
        return None


def describe(row):
    """{vendor, kind, why}: who probably made it, what it probably is (either may be None), and the evidence."""
    T = table()
    vendor = kind = None
    why = []
    address = str((row.get("keys") or [""])[0]).lower()
    public = row.get("addr_type") == "public"
    mfr = [(int(c), str(h or "")) for c, h in (row.get("mfr") or [])]
    svc = [str(s).lower() for s in (row.get("svc") or [])]
    sdata = {str(k).lower(): str(v) for k, v in (row.get("sdata") or {}).items()}

    def prefix_vendor():
        name = T["oui"].get(address.replace(":", "")[:6]) if public else None
        if name:
            why.append("address prefix " + address[:8].upper())
        return name

    if row.get("ibeacon"):
        kind = "iBeacon"
        why.append("iBeacon")
    for cid, h in mfr:
        t = _first_byte(h)
        if cid == APPLE and t in APPLE_TYPES and not row.get("ibeacon"):
            vendor, kind = "Apple", APPLE_TYPES[t]
            why.append(f"Apple continuity message 0x{t:02X}")
        elif cid == MICROSOFT and t in MICROSOFT_TYPES and kind is None:
            vendor, kind = "Microsoft", MICROSOFT_TYPES[t]
            why.append(f"Microsoft beacon 0x{t:02X}")
    if FAST_PAIR in svc or FAST_PAIR in sdata:
        model = sdata.get(FAST_PAIR, "")
        kind = kind or ("Fast Pair accessory, model " + model.upper() if len(model) == 6 else "Fast Pair accessory")
        why.append("Google Fast Pair")
    for s in svc + list(sdata):
        if kind is None and s in SERVICE_KINDS:
            kind = SERVICE_KINDS[s]
            why.append("service " + s.upper())

    if vendor is None and row.get("ibeacon"):
        vendor = prefix_vendor()                       # Apple's id in an iBeacon says nothing about the maker
    if vendor is None:
        for cid, _ in mfr:
            if cid == APPLE and row.get("ibeacon"):
                continue
            name = T["company"].get(str(cid))
            if name:
                vendor = name
                why.append(f"manufacturer ID 0x{cid:04X}")
                break
            why.append(f"manufacturer ID 0x{cid:04X} (not registered)")
    if vendor is None:
        for s in svc + list(sdata):
            if s in T["member"]:
                vendor = T["member"][s]
                why.append("service " + s.upper())
                break
    if vendor is None and not row.get("ibeacon"):
        vendor = prefix_vendor()
    return dict(vendor=vendor, kind=kind, why=why)
