"""Receivers - the Bluetooth proxies and Echo Shows - in the house document (spec
docs/specs/2026-10-01-receiver-placement-design.md).

In the document a receiver is placed by floor id, room id, x, y (metres) and height above its floor, with its moves
(where it stood until when) and epochs (when it was re-seated, re-oriented or swapped). The model keeps reading the
`live_scanners` dictionary it always has: `legacy(doc)` builds it from the document; a house without receivers
reads the registry its installation names (RP_REGISTRY - Nick's: solver/scanners_v2.json, the registry his receivers
were converted from, `from_registry`), and has none otherwise."""
import json
import os
import re
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
# registry fields the document models; any other field is kept verbatim in `extra`
MODELLED = {"floor", "x", "y", "z_rel", "z_abs", "room", "mac", "model", "ble_source", "replaced_at", "moved_at",
            "previous", "note", "added_at"}


def _r6(v):
    return round(float(v), 6)


def registry_path():
    """The registry file RP_REGISTRY names (relative to the repo root, like RP_FRESH), or None."""
    v = os.environ.get("RP_REGISTRY")
    return None if not v else (v if os.path.isabs(v) else os.path.join(HERE, "..", v))


def registry_file():
    """The named registry's receivers, or {} without one (a new household)."""
    path = registry_path()
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)["live_scanners"]


def _as_list(v, kind):
    if not v:
        return []
    return [v] if isinstance(v, kind) else list(v)


def from_registry(doc, registry, skip_unknown=False):
    """The registry's placed receivers as document receivers (rooms by name -> id on the receiver's floor; heights
    above the floor such that elevation + height gives back the registry's absolute height). A receiver on a floor
    the house does not have is an error, or left out with skip_unknown (another house than the registry's)."""
    floors = {f["id"]: f for f in doc["floors"]}
    out, taken = [], set()
    for name, v in registry.items():
        if v.get("x") is None:
            continue
        f = floors.get(v.get("floor"))
        if f is None:
            if skip_unknown:
                continue
            raise ValueError(f"{name}: floor {v.get('floor')!r} is not in the house")
        elev = float(f["elevation"])
        z_abs = v["z_abs"] if v.get("z_abs") is not None else elev + float(v.get("z_rel") or 0)
        rid = base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "receiver"
        n = 1
        while rid in taken:
            n += 1
            rid = f"{base}_{n}"
        taken.add(rid)
        moves = []
        ats, prevs = _as_list(v.get("moved_at"), str), _as_list(v.get("previous"), dict)
        for at, p in sorted(zip(ats, prevs), key=lambda ap: datetime.fromisoformat(ap[0]).timestamp()):
            pf = floors.get(p.get("floor") or f["id"], f)
            pz = p["z_abs"] if p.get("z_abs") is not None else float(pf["elevation"]) + float(p.get("z_rel") or 0)
            m = dict(at=at, x=p["x"], y=p["y"], height=_r6(pz - float(pf["elevation"])))
            if p.get("floor") and p["floor"] != f["id"]:
                m["floor"] = p["floor"]
            moves.append(m)
        r = dict(id=rid, name=name, model=v.get("model"), mac=v.get("mac"), address=v.get("ble_source"),
                 floor=f["id"], room=next((x["id"] for x in f.get("rooms") or [] if x.get("name") == v.get("room")), None),
                 x=v["x"], y=v["y"], height=_r6(z_abs - elev), epochs=_as_list(v.get("replaced_at"), str), moves=moves,
                 note=v.get("note"), added=v.get("added_at"),
                 extra={k: val for k, val in v.items() if k not in MODELLED})
        out.append({k: val for k, val in r.items() if val is not None and val != {}})
    return out


def legacy(doc):
    """The document's receivers as the model's `live_scanners` dictionary {name: {...}}: absolute heights, room names,
    `replaced_at`, `moved_at` + `previous`. Receivers on a floor the document does not have are left out (the
    problems list names them)."""
    floors = {f["id"]: f for f in doc.get("floors") or []}

    def names(f):
        return {x.get("id"): x.get("name") for x in f.get("rooms") or []}

    out = {}
    for r in doc.get("receivers") or []:
        f = floors.get(r.get("floor"))
        if f is None:
            continue
        elev = float(f["elevation"])
        v = dict(r.get("extra") or {})
        v.update(floor=f["id"], x=r["x"], y=r["y"], z_rel=r["height"], z_abs=_r6(elev + float(r["height"])),
                 room=names(f).get(r.get("room")))
        for mine, theirs in (("mac", "mac"), ("model", "model"), ("address", "ble_source"), ("note", "note"),
                             ("added", "added_at")):
            if r.get(mine) is not None:
                v[theirs] = r[mine]
        if r.get("epochs"):
            v["replaced_at"] = list(r["epochs"])
        if r.get("moves"):
            prev = []
            for m in r["moves"]:
                mf = floors.get(m.get("floor") or f["id"], f)
                p = dict(x=m["x"], y=m["y"], z_rel=m["height"], z_abs=_r6(float(mf["elevation"]) + float(m["height"])))
                if mf["id"] != f["id"]:
                    p["floor"] = mf["id"]
                if m.get("room"):
                    p["room"] = names(mf).get(m["room"])
                prev.append(p)
            v["previous"], v["moved_at"] = prev, [m["at"] for m in r["moves"]]
        out[r["name"]] = v
    return out


def registry(doc=None):
    """What the model reads as the receivers: the document's (doc None = the live house) when it has any, else the
    named registry's (none without one)."""
    if doc is None:
        import house_doc as HD
        try:
            doc = HD.load(HD.default_path())
        except Exception:
            doc = {}
    return legacy(doc) if doc.get("receivers") else registry_file()


def build_key(doc):
    """The maps' build key: the document with receivers and anchors emptied, so moving a receiver reuses the maps."""
    import house_doc as HD
    return HD.doc_hash({**doc, "receivers": [], "anchors": []})


def _iso(s):
    try:
        datetime.fromisoformat(s)
        return True
    except (TypeError, ValueError):
        return False


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def problems(doc):
    """Schema problems of the document's receivers, as sentences."""
    out = []
    rs = doc.get("receivers", [])
    if not isinstance(rs, list):
        return ["receivers must be a list"]
    floors = {f.get("id"): f for f in doc.get("floors") or []}
    for r in rs:
        if not isinstance(r, dict):
            out.append("a receiver must be an object")
            continue
        who = r.get("name") or r.get("id") or "a receiver"
        if not isinstance(r.get("name"), str) or not r["name"].strip():
            out.append(f"receiver {r.get('id')}: no name")
        f = floors.get(r.get("floor"))
        if f is None:
            out.append(f"receiver {who}: floor {r.get('floor')!r} is not in the house")
        elif r.get("room") is not None and r["room"] not in {x.get("id") for x in f.get("rooms") or []}:
            out.append(f"receiver {who}: room {r['room']!r} is not on floor {f.get('id')}")
        for k in ("x", "y", "height"):
            if not _num(r.get(k)):
                out.append(f"receiver {who}: {k} must be a number")
        ep = r.get("epochs", [])
        if not isinstance(ep, list) or not all(_iso(e) for e in ep):
            out.append(f"receiver {who}: every epoch must be a date and time (ISO 8601)")
        mv = r.get("moves", [])
        if not isinstance(mv, list) or not all(isinstance(m, dict) and _iso(m.get("at")) and _num(m.get("x"))
                                               and _num(m.get("y")) and _num(m.get("height"))
                                               and (m.get("floor") is None or m["floor"] in floors) for m in mv):
            out.append(f"receiver {who}: every move needs a time (ISO 8601), x, y, height and a floor of this house")
        else:
            for m in mv:                            # a gone room would nudge the old spot unconstrained
                mf = floors.get(m.get("floor") or r.get("floor"))
                if m.get("room") is not None and mf is not None and \
                        m["room"] not in {x.get("id") for x in mf.get("rooms") or []}:
                    out.append(f"receiver {who}: the move of {m.get('at')} names room {m['room']!r}, not on its floor")
    return out
