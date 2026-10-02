"""Convergence check: have we collected enough calibration data? (spec section 6)

  python convergence.py --calib-dir /opt/rtls/sessions/calib --out checks/c...json [--newest r...]

Over every kept Calibrate-tab round plus the existing data:
  1. stability  - k refits, each on a random 80% of the calibration rounds; the spread of every fitted
                  constant (2 x delete-d jackknife SD). Unstable: scanner levels > 1 dB, n > 0.2,
                  anything else > 2 (its own units).
  2. saturation - held-out error when fitting on 50..100% of the training rounds, scored on a fixed
                  20% of rounds plus every validation round. Saturated when the 80% -> 100% step
                  improves the median error by < 0.1 m and room accuracy by < 2 points.
  3. rooms      - leave-one-ROUND-out error per point (a pole's tags always leave together), per room.
Also the headline (mid-slot, pocket height) error and "check this placement" surprises for the newest
round. It fits models only to measure them: the live model is never touched.
"""
import argparse
import json
import math
import os
import random
import statistics as st
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

MODEL = "DominantPath"
HINTS = {"SLAB": "rounds on other floors from the scanners", "n": "same-room rounds at varied distances",
         "W4m": "rounds behind interior walls", "W5m": "rounds through exterior walls (deck, patio, garage)",
         "W6m": "rounds behind glass", "W7m": "rounds behind closed interior doors",
         "W8m": "rounds behind the garage door", "Hm": "rounds behind the fridge, reef tank or cars",
         "A": "rounds far from every scanner", "gam": "rounds around corners and through doorways",
         "C": "rounds around corners"}


# ---- statistics (pure) --------------------------------------------------------------------
def jackknife_spread(values, m, n):
    """2 x the delete-d jackknife SD from subsamples of size m out of n."""
    if len(values) < 2 or n <= m:
        return math.inf
    return float(2.0 * st.stdev(values) * math.sqrt(m / (n - m)))


def limit_for(name):
    if name.startswith("K:"):
        return 1.0
    return 0.2 if name == "n" else 2.0


def hint_for(name):
    if name.startswith("K:"):
        return f"rounds near and far from {name[2:]}"
    return HINTS.get(name, f"more rounds exercising {name}")


def is_saturated(curve):
    by = {round(c["frac"], 2): c for c in curve}
    a, b = by.get(0.8), by.get(1.0)
    if not a or not b:
        return False
    return (a["median_err"] - b["median_err"] < 0.1) and (b["room_acc"] - a["room_acc"] < 2.0)


def surprises(results, round_id, factor=3.0):
    if not results:
        return []
    usual = st.median(r["err"] for r in results)
    out = []
    for r in results:
        if r["round"] != round_id:
            continue
        if not r["floor_ok"]:
            out.append(dict(round=r["round"], tag=r["tag"], err=round(r["err"], 2), reason="wrong floor"))
        elif r["err"] > factor * usual:
            out.append(dict(round=r["round"], tag=r["tag"], err=round(r["err"], 2),
                            reason=f"error {r['err']:.1f} m, over {factor:g}x the usual {usual:.1f} m"))
    return out


def worst_rooms(results, top=8):
    by = {}
    for r in results:
        by.setdefault((r["floor"], r["room"]), []).append(r["err"])
    rows = [dict(floor=f, room=rm, n=len(v), median_err=round(st.median(v), 2)) for (f, rm), v in by.items()]
    return sorted(rows, key=lambda r: -r["median_err"])[:top]


def headline(results):
    if not results:
        return {}
    mid = [r["err"] for r in results if r.get("slot") == "mid"]
    return dict(n=len(results), all_median_err=round(st.median(r["err"] for r in results), 2),
                mid_median_err=round(st.median(mid), 2) if mid else None,
                room_acc=round(100.0 * sum(r["room_ok"] for r in results) / len(results), 1),
                floor_acc=round(100.0 * sum(r["floor_ok"] for r in results) / len(results), 1))


def _write(path, obj):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


# ---- the check ----------------------------------------------------------------------------
def run(calib_dir, newest_round=None, out_path=None, seed=0, k=20, frac=0.8, min_rounds=4):
    t0 = time.time()
    out = dict(id=time.strftime("c%Y%m%dT%H%M%S", time.gmtime(t0)), round=newest_round, t=t0, status="ok",
               error=None, n_rounds=0, n_points=0)
    prev = os.environ.get("RP_CALIB_DIR")
    try:
        os.environ["RP_CALIB_DIR"] = calib_dir
        import bakeoff as B
        import calib_loader as CL
        import models as M
        D = B.Data()
        calib = [e for e in D.E if e.get("round")]
        train_cal = [e for e in calib if CL.trainable(e)]
        units = sorted({e["round"] for e in train_cal})
        out.update(n_rounds=len(units), n_points=len(calib))
        if len(units) < min_rounds:
            out["status"] = "not_enough"
            return _finish(out, out_path, t0)
        legacy = sorted({e["group"] for e in D.E if not e.get("round") and CL.trainable(e)})
        groups_of = {u: sorted({e["group"] for e in train_cal if e["round"] == u}) for u in units}

        def fit(us):
            return D.fit_all(legacy + [g for u in us for g in groups_of[u]], models=(MODEL,))[MODEL]

        def score(F, events):
            res = []
            for e in events:
                Ft = B.fit_at(F, e.get("t") or 0.0, D.replaced_t)      # levels in effect at capture time
                R = M.localise(MODEL, Ft, e["obs"], D.scanners, D.bank_for(e), D.meta, D.P, min_n=5, off_sd=12.0)
                p = M.summarise(R, D.house)
                sc = B.score(p, e["pos"][:2], e["fi"], e["room"])
                res.append(dict(id=e["id"], round=e["round"], tag=e["id"].split("/", 1)[1], slot=e.get("slot"),
                                room=e["room"], floor=e["fi"], err=float(sc["err"]),
                                floor_ok=bool(sc["floor_ok"]), room_ok=bool(sc["room_ok"])))
            return res

        # 1. stability
        rng = random.Random(seed)
        m = max(1, int(round(frac * len(units))))
        samples = {}
        for _ in range(k):
            F = fit(rng.sample(units, m))
            for s, v in F.K.items():
                if " (old" not in s:
                    samples.setdefault("K:" + s, []).append(float(v))
            for name, v in F.p.items():
                samples.setdefault(name, []).append(float(v))
        consts = []
        for name, vals in samples.items():
            spread = jackknife_spread(vals, m, len(units))
            lim = limit_for(name)
            consts.append(dict(name=name, value=round(st.median(vals), 2),
                               spread=None if math.isinf(spread) else round(spread, 2), limit=lim,
                               unstable=bool(math.isinf(spread) or spread > lim), hint=hint_for(name)))
        consts.sort(key=lambda c: (not c["unstable"], c["name"]))
        out["stability"] = dict(constants=consts, n_unstable=sum(c["unstable"] for c in consts), k=k, m=m)

        # 2. saturation
        order = units[:]
        random.Random(seed + 1).shuffle(order)
        n_test = max(1, int(round(0.2 * len(order))))
        test_u, pool = order[:n_test], order[n_test:]
        test_ev = [e for e in calib if e["round"] in test_u and not e.get("co_location")] + \
                  [e for e in calib if e.get("validation")]
        curve = []
        for f in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
            use = pool[:max(1, int(round(f * len(pool))))]
            r = score(fit(use), test_ev)
            curve.append(dict(frac=f, rounds=len(use), median_err=round(st.median(x["err"] for x in r), 3),
                              room_acc=round(100.0 * sum(x["room_ok"] for x in r) / len(r), 1)))
        out["saturation"] = dict(curve=curve, saturated=is_saturated(curve), test_rounds=test_u)

        # 3. leave-one-round-out, plus validation rounds scored on the full fit
        results = []
        for u in units:
            results += score(fit([x for x in units if x != u]), [e for e in train_cal if e["round"] == u])
        val = [e for e in calib if e.get("validation")]
        if val:
            results += score(fit(units), val)
        out["rooms"] = worst_rooms(results)
        out["headline"] = headline(results)
        out["surprises"] = surprises(results, newest_round or units[-1])
    except Exception as e:
        out.update(status="failed", error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-2000:])
    finally:
        if prev is None:
            os.environ.pop("RP_CALIB_DIR", None)
        else:
            os.environ["RP_CALIB_DIR"] = prev
    return _finish(out, out_path, t0)


def _finish(out, out_path, t0):
    out["seconds"] = round(time.time() - t0, 1)
    if out_path:
        _write(out_path, out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calib-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--newest", default=None)
    ap.add_argument("--k", type=int, default=20)
    a = ap.parse_args()
    out = run(a.calib_dir, newest_round=a.newest, out_path=a.out, k=a.k)
    print(json.dumps({k: out.get(k) for k in ("id", "status", "n_rounds", "seconds", "error")}))
    return 0 if out["status"] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
