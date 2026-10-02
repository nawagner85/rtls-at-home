"""Fresh-start training data (Nick 2026-09-25): fit only what was captured on today's proxy layout, not survey data
from layouts that no longer exist. bakeoff.events() returns these instead of the old events when RP_FRESH names a
fresh folder; solver/fresh_fit.py compares the two.

A fresh folder holds:
  snapshots/win_*.json  solver/capture_snapshots.py windows (engine GET /api/snapshot) of the fixed anchors
  calib/*.json          Calibrate-tab rounds taken on the same layout
  levels.json           {tag: dB above the phone} for the base tag - the only level carried over from older data:
                        a property of the tag's transmitter, not of the proxies
  refsnap_waps.json     the WAPs' readings in this data (write_wap_reference): the live anchor correction's reference

Events:
  - each fixed anchor at its measured position, one transmit offset per device; the three WAPs share one
    ("wap@fresh": same model, batch and TX settings, see waps_v1.json);
  - tags as known-level references (no offset): co-location rounds give every tag's level relative to the base tag,
    levels.json the base tag's level against the phone. A tag never co-located keeps its own free offset;
  - each co-location round as ONE event at the grid centre (per proxy, the median over its tags): tags 10 cm apart
    read up to 16 dB apart at one proxy (multipath), and seven near-identical events would bend the wall fit.
"""
import glob, json, os, statistics as st

import numpy as np

import calib_loader as CL
import geom3d as g

HERE = os.path.dirname(os.path.abspath(__file__))
WAPS_FILE = os.path.join(HERE, "waps_v1.json")


def _wap_file():
    """The installation's WAP file, or {} without one (a household with no WAP beacons)."""
    try:
        with open(WAPS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _waps():
    W = _wap_file()
    return {lbl: w for lbl, w in W.items() if not lbl.startswith("_")}, W.get("_mac", {})


def wap_keys_by_floor():
    """The WAP beacons' iBeacon keys by floor id (uuid_major_1, major from the "(major N)" label); {} without them."""
    W = _wap_file()
    if not W.get("_uuid"):
        return {}
    return {w["floor"]: f"{W['_uuid']}_{int(lbl.split('major')[1].strip(' )'))}_1"
            for lbl, w in W.items() if not lbl.startswith("_")}


def _fresh_dir():
    import bakeoff
    return bakeoff.fresh_dir()


def load_truth(fresh_dir):
    """The fresh folder's truth table: its fixed anchors (key, label, group, ogroup, floor id, x, y, h)."""
    path = os.path.join(fresh_dir, "truth.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path}: the fresh folder's truth table (its fixed anchors) is missing")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def truth_table(house, fresh_dir=None):
    """Every fixed anchor with its place: the fresh folder's (truth.json) and the WAP beacons (waps_v1.json)."""
    T = {}
    for a in load_truth(fresh_dir or _fresh_dir())["anchors"]:
        T[a["key"]] = dict(label=a["label"], group=a["group"], ogroup=a["ogroup"], fi=house.floor_index(a["floor"]),
                           xy=(a["x"], a["y"]), h=a["h"])
    keys = wap_keys_by_floor()
    for lbl, w in _waps()[0].items():
        fi = house.floor_index(w["floor"])
        T[keys[w["floor"]]] = dict(label=lbl, group="wap%d" % (fi + 1), ogroup="wap@fresh", fi=fi,
                                   xy=(w["x"], w["y"]), h=w["z_abs"] - house.Z[fi])
    for t in T.values():
        t["room"] = house.room_at(t["fi"], *t["xy"])
    return T


def load_windows(fresh_dir):
    return [json.load(open(f)) for f in sorted(glob.glob(os.path.join(fresh_dir, "snapshots", "win_*.json")))]


def combine(wins, key):
    """Per scanner: the median of the windows' medians, n summed; windows where the scanner was dead are skipped."""
    per = {}
    for w in wins:
        dead = set(w.get("dead") or ())
        for s, v in (w["snapshot"]["data"].get(key) or {}).items():
            if s not in dead:
                per.setdefault(s, []).append(v)
    return {s: dict(med=float(st.median(x["med"] for x in vs)), n=int(sum(x["n"] for x in vs)), windows=len(vs))
            for s, vs in per.items()}


def layout_stamps():
    """Every moment a scanner moved or was swapped (scanners_v2.json moved_at / replaced_at), as epoch seconds,
    sorted. Anchor windows either side of a stamp were heard on different layouts and must not be pooled."""
    from datetime import datetime
    ts = {datetime.fromisoformat(w).timestamp() for mv in g.move_timelines().values() for w, _ in mv}
    ts |= {datetime.fromisoformat(w).timestamp() for ws in g.replaced_scanners().values() for w in ws}
    return sorted(ts)


def window_epochs(wins, stamps):
    """The windows split into layout epochs (oldest first): consecutive windows with no stamp between them."""
    wins = sorted(wins, key=lambda w: w["snapshot"]["t0"])
    epochs, cur = [], []
    for w in wins:
        if cur and any(cur[-1]["snapshot"]["t0"] < t <= w["snapshot"]["t0"] for t in stamps):
            epochs.append(cur); cur = []
        cur.append(w)
    return epochs + ([cur] if cur else [])


def load_levels(fresh_dir):
    p = os.path.join(fresh_dir, "levels.json")
    return {k: float(v) for k, v in json.load(open(p)).items() if not k.startswith("_")} if os.path.exists(p) else {}


def _tag(e):
    return e["ogroup"].split(":", 1)[1]


def tag_offsets(cal, ref_offset, base=None):
    """Each tag's transmit level on the phone scale: co-location rounds give every tag's level relative to the base
    tag (median over proxies of the difference), ref_offset the base tag's own level - the base being the tag
    levels.json gives a level for."""
    base = base or (sorted(ref_offset)[0] if ref_offset else None)
    if base not in ref_offset:
        return {}
    rel = {base: [0.0]}
    for rid in sorted({e["round"] for e in cal if e.get("co_location")}):
        evs = {_tag(e): e for e in cal if e.get("round") == rid}
        b = evs.get(base)
        if b is None:
            continue
        for tag, e in evs.items():
            d = [e["obs"][s]["med"] - b["obs"][s]["med"] for s in e["obs"] if s in b["obs"]]
            if len(d) >= 3:
                rel.setdefault(tag, []).append(float(np.median(d)))
    return {t: ref_offset[base] + float(np.median(v)) for t, v in rel.items()}


def grid_event(evs, known):
    """A co-location round as one known-level event at the grid centre: per proxy, the median over the tags of the
    readings corrected to the phone scale."""
    evs = [e for e in evs if _tag(e) in known]
    if not evs:
        return None
    per = {}
    for e in evs:
        for s, v in e["obs"].items():
            per.setdefault(s, []).append((v["med"] - known[_tag(e)], v["n"]))
    obs = {s: dict(med=float(np.median([m for m, _ in vs])), n=int(sum(n for _, n in vs)))
           for s, vs in per.items() if len(vs) >= max(2, len(evs) // 2)}
    rid = evs[0]["round"]
    return dict(id=f"{rid}/grid", label=f"{len(evs)}-tag grid ({evs[0]['room']})", group=f"{rid}/grid", round=rid,
                kind="ref", xy=(round(float(np.mean([e["xy"][0] for e in evs])), 4),
                                round(float(np.mean([e["xy"][1] for e in evs])), 4)),
                fi=evs[0]["fi"], h=float(np.mean([e["h"] for e in evs])), room=evs[0]["room"], t=evs[0]["t"], obs=obs,
                validation=any(e.get("validation") for e in evs))     # a validation round is scored, never fit


def fresh_events(house, fresh_dir, ref_offset=None, log=print, stamps=None):
    """Every fresh event, positions nudged like bakeoff.events(). ref_offset: {tag: dB above the phone}; default
    the folder's levels.json. stamps: layout change times (default layout_stamps()); the anchor windows are one
    event per anchor per layout epoch, timed at that epoch's first window."""
    ref_offset = load_levels(fresh_dir) if ref_offset is None else ref_offset
    epochs = window_epochs(load_windows(fresh_dir), layout_stamps() if stamps is None else stamps)
    if len(epochs) > 1:
        log(f"  [fresh] anchor windows span {len(epochs)} proxy layouts: " +
            ", ".join(f"{len(ep)} from {ep[0]['snapshot']['t0']:.0f}" for ep in epochs))
    E = []
    for n, wins in enumerate(epochs):
        t0 = min(w["snapshot"]["t0"] for w in wins)
        for key, t in truth_table(house, fresh_dir).items():
            obs = combine(wins, key)
            if not obs:
                log(f"  [fresh] {t['label']}: not heard in the snapshots, left out")
                continue
            E.append(dict(id=f"anchor:{key}" + (f"@{n}" if n else ""), key=key, label=t["label"], group=t["group"],
                          kind="anchor", ogroup=t["ogroup"], xy=t["xy"], fi=t["fi"], h=t["h"], room=t["room"], t=t0,
                          obs={s: dict(med=v["med"], n=v["n"]) for s, v in obs.items()}))
    # the folder's rounds, then the installation's later captures (plan 2026-10-01-a4) - never the repo's older copy
    cal = CL.calib_events(house, os.path.join(fresh_dir, "calib"), also=CL.added_dir())
    known = tag_offsets(cal, ref_offset)
    if known:
        log("  [fresh] tag levels vs the phone: " + ", ".join(f"{t} {v:+.1f} dB" for t, v in sorted(known.items())))
    for e in cal:
        if not CL.trainable(e):
            continue
        if _tag(e) in known:
            obs = {s: dict(v, med=v["med"] - known[_tag(e)]) for s, v in e["obs"].items()}
            E.append(dict(e, kind="ref", label=e["id"], obs=obs))
        else:
            E.append(dict(e, label=e["id"]))
    for rid in sorted({e["round"] for e in cal if e.get("co_location")}):
        g = grid_event([e for e in cal if e.get("round") == rid], known)
        if g:
            E.append(g)
    for e in E:
        x, y, z, _ = house.nudge(e["xy"][0], e["xy"][1], house.Z[e["fi"]] + e["h"], room=e["room"])
        e["pos"] = (x, y, z)
    return E


def write_wap_reference(fresh_dir, stamps=None):
    """The WAPs' combined readings as <fresh_dir>/refsnap_waps.json, in survey/refsnap_waps.json's shape (keyed by
    WAP MAC): the reference the live anchor correction compares against, so it measures drift from THIS data.
    Only the latest layout epoch counts: the live proxies stand where that epoch's windows were heard."""
    wins = window_epochs(load_windows(fresh_dir), layout_stamps() if stamps is None else stamps)[-1]
    waps, macs = _waps()
    data = {}
    for lbl, w in waps.items():
        obs = combine(wins, wap_keys_by_floor()[w["floor"]])
        if obs:
            data[macs[lbl]] = {s: dict(med=v["med"], n=v["n"]) for s, v in obs.items()}
    path = os.path.join(fresh_dir, "refsnap_waps.json")
    with open(path, "w") as f:
        json.dump(dict(t0=min(w["snapshot"]["t0"] for w in wins),
                       seconds=sum(w["snapshot"]["seconds"] for w in wins), data=data), f, indent=1)
    return path
