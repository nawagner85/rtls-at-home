"""Competing BLE localisation models, one shared statistical layer.

Physics arms (all 3-D, all with a per-scanner constant K_s that absorbs REF, the
phone's TX power AND each receiver's bias - the term that wrecked both blind tests):

  FreeSpace     K_s - 10 n log10(d3) - SLAB*kslab                         (control)
  Walls         + linear penalty per crossing (Motley-Keenan multi-wall)
  CappedWalls   obstruction saturates: A(1 - exp(-L/A))  (COST-231-style; long
                multi-wall paths stop accruing loss because energy finds another way)
  DominantPath  soft-max of the capped penetration path and a door-routed path
                (geodesic through doorways, detour and corner penalties)

Baselines:
  Bermuda           strongest raw scanner -> that scanner's room
  Bermuda+offsets   strongest bias-corrected scanner (what its rssi_offsets setting does)

Shared layer: robust Student-t likelihood, censored non-detections (a scanner that
cannot hear you is evidence too - Bermuda ignores it), a global offset nuisance for
phone orientation / pocket / TX drift, posterior over every cell on all three floors.
"""
import math, os
import numpy as np
import json
from scipy.optimize import least_squares
from scipy.special import logsumexp, log_ndtr

ALL_PHYS = ["FreeSpace", "Walls", "CappedWalls", "DominantPath", "FreeSpace+echo", "DominantPath+echo"]
PHYS = ["FreeSpace", "Walls", "CappedWalls", "DominantPath"]          # v1 set (kept for reproducibility)
MODELS = ["Bermuda", "Bermuda+offsets"] + PHYS

#            mean   sd    lo     hi
PRIOR = {"n":    (2.0, 0.4, 1.2, 4.0),
         "W4":   (4.0, 3.0, 0.0, 20.0),     # interior stud wall; 10 dB/wall was fit to a broken proxy
         "W5":   (8.0, 5.0, 0.0, 30.0),     # exterior wall
         "W6":   (2.0, 3.0, 0.0, 15.0),     # glass
         "W7":   (3.0, 2.0, 0.0, 12.0),     # interior door
         "W8":   (15.0, 6.0, 0.0, 40.0),    # metal garage door
         "H":    (6.0, 5.0, 0.0, 30.0),     # high fixture: metal, water (fridge, cars, reef tank)
         "WL":   (1.0, 1.5, 0.0, 10.0),     # low furniture: sofa, bed, desk, nightstand
         "WM":   (2.5, 2.0, 0.0, 15.0),     # med furniture: cabinetry, shelving, appliances
         # MEASURED three ways: BLE differential 1.8/2.1 dB (2026-09-21) and AP-to-AP RF scan
         # 2.2 dB (2026-09-22, known geometry at BOTH ends - the strongest evidence we have).
         # RP_SLABFREE=1 restores the loose prior. Tight by default because a free SLAB was acting
         # as a catch-all, inflating to 4.0 while the wall terms collapsed to 1.28 / 0.00 dB.
         # TESTED 2026-09-23: forcing SLAB to the 2.2 dB measured between APs made held-out accuracy
         # WORSE and the fit jammed against the bound at 3.92 (5.7 sd above the prior). The BLE data
         # genuinely wants ~4 dB per floor crossed. Likely the AP-to-AP figure is the biased one: the
         # three WAPs sit in VERTICALLY STACKED HALLWAYS joined by stairwells, so their mutual paths
         # can route through the openings instead of through the assembly, understating it. Keep the
         # loose prior; "SLAB" is an effective per-floor term, not a pure material property.
         "SLAB": (2.0, 1.0, 0.0, 10.0),
         "A":    (20.0, 10.0, 3.0, 60.0),
         "gam":  (1.0, 1.0, 0.0, 6.0),      # dB per metre of detour
         "C":    (6.0, 4.0, 0.0, 20.0),     # corner (diffraction) penalty
         "dne":  (0.0, 0.5, -1.5, 2.0)}     # v2: extra path-loss exponent for Echo Show receivers
K_PRIOR = (-62.5, 8.0, -95.0, -35.0)        # REF measured ~ -62.5 on the ESP32 scale
O_PRIOR = (0.0, 12.0, -45.0, 45.0)          # anchors: unknown TX power

# WL/WM are only in the arms that model obstructions at all; FreeSpace stays a pure control.
# RP_FURN=1 enables them. DEFAULT OFF: tested 2026-09-23 on 28 events, identical data both ways,
# and they made held-out accuracy WORSE (DominantPath 11/28 -> 9/28 room, 23/28 -> 21/28 floor).
# The optimizer found ~1 dB of furniture attenuation (WL 0.84, WM 1.14) but paid for it by pulling
# the interior-wall term down from 1.28 to 0.72 dB - it absorbs wall loss rather than adding
# information. Revisit when there are far more survey points; the features are emitted either way.
FURN = os.environ.get("RP_FURN", "0") == "1"
_F = ["WL", "WM"] if FURN else []
# RP_NCLUTTER (2026-09-27): the distance exponent grows with the obstacle count along the path, n + dn per wall or
# fixture crossing. The office plates by the tank read the living-room Show ~10 dB louder than one exponent allows:
# a long path through the open kitchen/living volume falls off slower than a cluttered one of the same length.
NCLUTTER = os.environ.get("RP_NCLUTTER", "0") == "1"
_N = ["dn"] if NCLUTTER else []
# RP_NROUTE (2026-09-27): the routed path gets its own distance exponent (a corridor falls off like free space);
# the house-wide n stays on the straight line. From the office north wall the straight line to the living-room
# Show passes the fridge yet arrives at free-space strength: the signal goes round, and at n 2.5 + detour + corner
# the route can never win.
NROUTE = os.environ.get("RP_NROUTE", "0") == "1"
# RP_NRX (2026-09-27): a per-receiver distance exponent offset (n + x_s), prior 0 +- 0.3 - a receiver that falls
# off slower or faster than the house average (a directional front, a placement in the open volume).
NRX = os.environ.get("RP_NRX", "0") == "1"
# RP_AZGAIN (2026-09-27): a per-Show azimuth gain a cos(az) + b sin(az) dB, az the bearing receiver -> device. The
# Great Room Show sits beside the fireplace chase: clear one way, not the other (Nick), and hci0 beside it heard
# the office 4-7 dB worse and the hallway WAPs 7-8 dB better than the Show did. Two linear parameters per Show.
AZGAIN = os.environ.get("RP_AZGAIN", "0") == "1"
# RP_AZLOBE (Nick 2026-09-27, "start angle, end angle, and magnitude"): cos = a cos(az) + b sin(az) (linear);
# sector = a gain G inside a sector [c - hw, c + hw] with soft 10-degree edges, three parameters per Show (G, c, hw).
# RP_AZCAP bounds the magnitude: 3, 10 or off (a wide bound). The DOE runs both lobes at each cap.
AZLOBE = os.environ.get("RP_AZLOBE", "cos")
_azcap = os.environ.get("RP_AZCAP", "12")
AZ_CAP = 60.0 if _azcap == "off" else float(_azcap)
AZ_PRIOR = (0.0, min(3.0, AZ_CAP), -AZ_CAP, AZ_CAP)             # cos: a, b
AZS_PRIOR = {"G": (0.0, min(3.0, AZ_CAP), 0.0, AZ_CAP),          # sector: gain dB
             "c": (90.0, 60.0, -270.0, 270.0),                    # centre bearing, degrees (0 east, 90 north)
             "hw": (60.0, 40.0, 15.0, 180.0)}                     # half-width, degrees
AZ_EDGE = 10.0                                                    # degrees: softness of the sector's edges
# RP_DIPOLE (2026-09-28): the XIAO proxies stand their 3 dBi dipoles vertically, so they are deaf straight up and down.
# Links near vertical read weaker than predicted (-3.7 dB on average within 30 degrees; a proxy hears ceiling
# lights straight above it 9-13 dB weaker). Nick: the running corrections handle drift from real life, not a
# static error like this. One fitted weight Dw scales the ideal half-wave pattern; reflections fill the real null.
DIPOLE = os.environ.get("RP_DIPOLE", "0") == "1"
DIPOLE_PRIOR = (0.3, 0.3, 0.0, 1.0)                               # weight: mean, sd, lo, hi
DIPOLE_FLOOR = -25.0                                              # dB: the ideal null is -inf straight up and down
_DIPOLES = None


def dipole_receivers():
    """Receivers with a vertical dipole: the XIAO boards in the registry (their '(oldN)' epochs were ESP32s)."""
    global _DIPOLES
    if _DIPOLES is None:
        import house_receivers as HR
        _DIPOLES = {s for s, v in HR.registry().items() if "XIAO" in str(v.get("model", ""))}
    return _DIPOLES


def is_dipole(scanner):
    return scanner in dipole_receivers()


def sector_weight(ax, ay, c_deg, hw_deg):
    """1 inside the sector centred at c with half-width hw (soft edges), 0 outside; ax, ay the unit bearing."""
    az = np.degrees(np.arctan2(ay, ax))
    d = np.abs((az - c_deg + 180.0) % 360.0 - 180.0)              # angular distance to the centre
    return 1.0 / (1.0 + np.exp((d - hw_deg) / AZ_EDGE))
X_PRIOR = (0.0, 0.3, -1.0, 1.0)
_N = _N + (["nr"] if NROUTE else [])
PRIOR["nr"] = (2.0, 0.4, 1.2, 4.0)
PRIOR["dn"] = (0.0, 0.3, -1.0, 1.0)
USES = {"FreeSpace": ["n", "SLAB"],
        "Walls": ["n", "W4", "W5", "W6", "W7", "W8", "H"] + _F + ["SLAB"],
        "CappedWalls": ["n", "W4", "W5", "W6", "W7", "W8", "H"] + _F + ["SLAB", "A"],
        "DominantPath": ["n", "W4", "W5", "W6", "W7", "W8", "H"] + _F + _N + ["SLAB", "A", "gam", "C", "Wat"]}
# v2 hypothesis (from blind test 2, post-hoc): Echo Shows read hot up close and cold far away,
# i.e. a steeper falloff than the ESP32s - which a constant per-scanner offset cannot express.
USES["FreeSpace+echo"] = USES["FreeSpace"] + ["dne"]
USES["DominantPath+echo"] = USES["DominantPath"] + ["dne"]


# Per-METRE variants. A stud wall is ~0.10 m in the raster, so 4 dB/crossing ~ 40 dB/m.
PRIOR.update({"W4m": (40.0, 25.0, 0.0, 250.0), "W5m": (60.0, 40.0, 0.0, 400.0),
              "W6m": (20.0, 15.0, 0.0, 200.0), "W7m": (25.0, 15.0, 0.0, 200.0),
              "W8m": (120.0, 60.0, 0.0, 600.0), "Hm": (10.0, 8.0, 0.0, 80.0)})
# DEFAULT ON since 2026-09-23: metres-of-material took DominantPath's held-out median error from
# 2.5 m to 1.9 m on 28 events AND gave a physically sensible wall coefficient (45 dB/m = 4.5 dB per
# 0.10 m stud wall) where the crossing count had collapsed to 1.35 dB. Nick's insight: a crossing
# count cannot express incidence angle; path length captures t/cos(theta) for free.
PATHLEN = os.environ.get("RP_PATHLEN", "1") == "1"
GRAZE = os.environ.get("RP_GRAZE", "0") == "1"
SLABLEN = os.environ.get("RP_SLABLEN", "0") == "1"
# TESTED 2026-09-23 and REJECTED (default off): routing cross-floor links through the open
# stairwell with no slab penalty reconciled SLAB to the measured 2.2 dB and gave the best in-sample
# fit, but held-out got WORSE where it matters - validation tags 1.6 -> 3.5 m, WAP anchors 2.4 -> 4.9 m.
# The shaft is open but not free: railings, turns and the enclosed flight sides still cost signal,
# so a zero-loss route over-credits every path that can reach the stairs.
STAIRROUTE = os.environ.get("RP_STAIRROUTE", "0") == "1"
PRIOR["SLABm"] = (12.0, 8.0, 0.0, 60.0)      # dB per metre of assembly (4 dB / 0.33 m)
# Water (2026-09-25): dB per body of water crossed below its surface (reef tank, ATO reservoir). Flat, not per metre:
# 2.4 GHz barely penetrates water, so the loss is set by how the signal gets around it. Taken off the final
# prediction, outside the DominantPath cap A (fitted near 3 dB). Measured: a proxy hears the plate over the
# tank ~7 dB below the corner plate at the same height, 0.5 m farther; the prior is physics, the fit refines it.
PRIOR["Wat"] = (20.0, 8.0, 0.0, 45.0)
# The DominantPath cap A squashes the direct path's summed wall/fixture loss. On the fresh data the fit drives A to its
# 3 dB lower bound, which makes every dense thing (fridge, cars, the homelab rack) nearly irrelevant (Nick 2026-09-26:
# "the 3 dB cap is a problem"). RP_A=<dB> pins it (sd ~0); RP_A=off removes the cap. Experiment switch.
A_MODE = os.environ.get("RP_A", "")
if A_MODE and A_MODE != "off":
    PRIOR["A"] = (float(A_MODE), 1e-3, float(A_MODE) - 1e-3, float(A_MODE) + 1e-3)
if SLABLEN:
    for _m in USES:
        USES[_m] = [x if x != "SLAB" else "SLABm" for x in USES[_m]]
PRIOR["Gm"] = (8.0, 8.0, 0.0, 60.0)          # dB per Fresnel-weighted metre alongside a wall
# Horizontal plates (geom3d.PLATES; features need RP_RAYS=exact): dB per metre of furniture TOP crossed.
# Broad priors: ~3 cm of wood ~ 2 dB, of stone ~ 5 dB, a metal sheet ~ 20+ dB.
PRIOR.update({"PWm": (60.0, 60.0, 0.0, 600.0), "PSm": (150.0, 150.0, 0.0, 1500.0),
              "PMm": (700.0, 700.0, 0.0, 5000.0)})
PLATES = os.environ.get("RP_PLATES", "0") == "1"
if PLATES:
    USES["DominantPath"] = USES["DominantPath"] + ["PWm", "PSm", "PMm"]
if GRAZE:
    for _m in ("Walls", "CappedWalls", "DominantPath"):
        USES[_m] = USES[_m] + ["Gm"]
if PATHLEN:
    for _m in ("Walls", "CappedWalls", "DominantPath"):
        USES[_m] = [x if x not in ("W4", "W5", "W6", "W7", "W8", "H") else x + "m" for x in USES[_m]]


# Echo Shows: receiver level wanders ~7 dB over hours (ESP32s ~1 dB). Added in quadrature to the
# residual sigma wherever a Show link is scored. RP_ECHO_SD=0 restores uniform sigma for A/B.
ECHO_EXTRA_SD = float(os.environ.get("RP_ECHO_SD", "4.0"))
# 0.5 chosen 2026-09-23 on the 9 current-layout beacons: within-r68 coverage 78-89% (target 68%),
# median r68 5.4 -> 3.3 m, room accuracy held/improved, confident calls 3/9 -> 5/9. 0.4 was equally
# calibrated but is not worth the overconfidence risk on nine points.
SIGMA_SCALE = float(os.environ.get("RP_SIGMA_SCALE", "0.5"))


def is_echo(scanner):
    return "Show" in str(scanner)


# Echo Shows hear by direction (2026-09-26, solver/echo_dir_diag.py): on the fresh fit they read louder than predicted
# on their own floor and weaker above and below (Great Room: same floor +4.2, floor below -3.3, above -2.3) - a
# horizontal antenna pattern, and the Great Room Show sits on the homelab cabinet. Each Show gets a vertical gain
# e_s x |sin(elevation)|, in every model but FreeSpace (the pure control).
# TESTED 2026-09-26 and REJECTED (default off, RP_VGAIN=1 to try): held out, 22 events, DominantPath 19/22 rooms,
# 22/22 floors, 1.5 m without it; 18/22, 22/22, 1.8 m with it; the north-office points (3.7-4.0 m south) unmoved.
VGAIN = os.environ.get("RP_VGAIN", "0") == "1"
E_PRIOR = (0.0, 6.0, -20.0, 10.0)          # dB per unit |sin(elevation)|


def vert(f):
    """|sin(elevation)| from a receiver to each point: |dz| / distance."""
    return f["dz"] / np.maximum(f["d3"], 0.5)


def dipole_db(f):
    """A vertical half-wave dipole's gain relative to broadside, per link: 20 log10 |cos(pi/2 cos th) / sin th|, th the
    angle from the vertical axis (cos th = vert(f)); floored at DIPOLE_FLOOR."""
    c = np.clip(np.asarray(vert(f), float), 0.0, 1.0)
    s = np.sqrt(np.maximum(1.0 - c * c, 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        g = np.abs(np.cos(np.pi / 2 * c)) / np.maximum(s, 1e-9)
        db = 20.0 * np.log10(np.maximum(g, 1e-12))
    return np.clip(db, DIPOLE_FLOOR, 0.0)


def mu_s(model, F, s, f):
    """mu for receiver s under fit F, with that receiver's vertical gain when it has one."""
    px = getattr(F, "x", {})
    v = mu(model, dict(F.p, dnx=px[s]) if s in px else F.p, F.K[s], f)
    az = getattr(F, "az", {}).get(s)
    if az and "ax" in f:
        v = v + az[0] * f["ax"] + az[1] * f["ay"]
    sec = getattr(F, "azs", {}).get(s)
    if sec and "ax" in f:
        v = v + sec[0] * sector_weight(f["ax"], f["ay"], sec[1], sec[2])
    dw = getattr(F, "dw", None)
    if dw and is_dipole(s):
        v = v + dw * dipole_db(f)
    g = getattr(F, "e", {}).get(s)
    return v + g * vert(f) if g else v


def extra_sd(scanner):
    return ECHO_EXTRA_SD if is_echo(scanner) else 0.0


def mu(model, p, K, f):
    """Predicted RSSI for one scanner against a feature dict of arrays."""
    n_eff = p["n"] + p.get("dne", 0.0) * f.get("echo", 0.0) + p.get("dnx", 0.0)   # dnx: RP_NRX per-row receiver offset
    if "dn" in p:                                          # RP_NCLUTTER: + dn per wall / fixture crossing
        n_eff = n_eff + p["dn"] * (f.get("k4", 0.0) + f.get("k5", 0.0) + f.get("kh", 0.0))
    model = model.replace("+echo", "")
    logd = np.log10(np.maximum(f["d3"], 0.5))
    if model == "FreeSpace":
        return K - 10 * n_eff * logd - (p["SLABm"] * f["mslab"] if SLABLEN else p["SLAB"] * f["kslab"])
    if PATHLEN:
        L = (p["W4m"] * f["m4"] + p["W5m"] * f["m5"] + p["W6m"] * f["m6"] + p["W7m"] * f["m7"]
             + p["W8m"] * f["m8"] + p["Hm"] * f["mh"])
    else:
        L = (p["W4"] * f["k4"] + p["W5"] * f["k5"] + p["W6"] * f["k6"] + p["W7"] * f["k7"]
             + p["W8"] * f["k8"] + p["H"] * f["kh"])
    L = (L + p.get("WL", 0.0) * f.get("klow", 0.0) + p.get("WM", 0.0) * f.get("kmed", 0.0)
         + p.get("Gm", 0.0) * f.get("mgraze", 0.0)
         + p.get("PWm", 0.0) * f.get("mpw", 0.0) + p.get("PSm", 0.0) * f.get("mps", 0.0)
         + p.get("PMm", 0.0) * f.get("mpm", 0.0))
    if model in ("CappedWalls", "DominantPath") and A_MODE != "off":
        L = p["A"] * (1.0 - np.exp(-L / p["A"]))
    wat = p.get("Wat", 0.0) * f.get("kwat", 0.0)
    pen = K - 10 * n_eff * logd - L - (p["SLABm"] * f["mslab"] if SLABLEN else p["SLAB"] * f["kslab"])
    if model != "DominantPath":
        return pen - wat
    G = f["G"]
    detour = np.where(np.isfinite(G), np.maximum(G - f["oct"], 0.0), 0.0)
    dr = np.sqrt((f["dxy"] + detour) ** 2 + f["dz"] ** 2)
    n_r = p.get("nr", n_eff)                                # RP_NROUTE: the route's own exponent
    route = (K - 10 * n_r * np.log10(np.maximum(dr, 0.5)) - p["gam"] * detour
             - p["C"] * (detour > 0.3) - (p["SLABm"] * f["mslab"] if SLABLEN else p["SLAB"] * f["kslab"]))
    route = np.where(np.isfinite(G), route, pen - 60.0)
    if STAIRROUTE and "Gx" in f:
        # Cross-floor: walk to the stairs, take the flight, walk on. Open shaft, so no SLAB.
        Gx = f["Gx"]
        cross = (f["kslab"] > 0) & np.isfinite(Gx)
        extra = np.maximum(Gx - f["d3"], 0.0)
        route_x = K - 10 * n_eff * np.log10(np.maximum(Gx, 0.5)) - p["gam"] * extra
        route = np.where(cross, route_x, route)
    # Combination temperature. tau=10 is a TRUE power sum of the two paths (parallel conductances,
    # per Nick's resistor-network framing); tau=2 is a much harder max. RP_TAU overrides for A/B.
    tau = float(os.environ.get("RP_TAU", "2.0"))
    m = np.maximum(pen, route)
    return m + tau * np.log10(10 ** ((pen - m) / tau) + 10 ** ((route - m) / tau)) - wat


class Fit:
    def __init__(self, model, scanners, anchors, theta, names, resid):
        self.model, self.scanners, self.anchors = model, scanners, anchors
        self.names, self.theta = names, theta
        self.p = {k: theta[names.index(k)] for k in USES[model]}
        self.K = {s: theta[names.index("K:" + s)] for s in scanners}
        self.o = {a: theta[names.index("o:" + a)] for a in anchors}
        self.gup = theta[names.index("gup")] if "gup" in names else None
        self.dw = theta[names.index("Dw")] if "Dw" in names else None                  # RP_DIPOLE weight
        self.e = {n[2:]: theta[i] for i, n in enumerate(names) if n.startswith("e:")}     # per-Show vertical gain
        self.x = {n[2:]: theta[i] for i, n in enumerate(names) if n.startswith("x:")}     # per-receiver exponent offset
        self.az = {n[3:]: (theta[i], theta[names.index("as:" + n[3:])]) for i, n in enumerate(names) if n.startswith("ac:")}
        self.azs = {n[3:]: (theta[i], theta[names.index("az:" + n[3:])], theta[names.index("aw:" + n[3:])])
                    for i, n in enumerate(names) if n.startswith("ag:")}                              # sector lobe (G, centre, half-width)
        self.resid = resid
        r = np.asarray(resid)
        self.mad = float(1.4826 * np.median(np.abs(r - np.median(r)))) if len(r) else 5.0
        self.rms = float(np.sqrt(np.mean(r ** 2))) if len(r) else 5.0

    def bias(self, ref=None):
        """Per-scanner offset relative to a reference receiver - Bermuda rssi_offset = -bias. The reference is
        RP_BIAS_REF (an installation's choice), else the first ESP32 receiver by name, else the first; offsets are
        relative, so the choice shifts them all alike (the Bermuda comparison's argmax doesn't change)."""
        ref = ref or os.environ.get("RP_BIAS_REF")
        if ref and ref not in self.K:
            print(f"bias: no receiver {ref!r} to refer to; using another")
            ref = None
        ref = ref or next((s for s in sorted(self.scanners) if not is_echo(s)), None) or \
            (sorted(self.scanners)[0] if self.scanners else None)
        return {s: self.K[s] - self.K[ref] for s in self.scanners} if ref else {}


# ---- the default model (spec 2026-10-01-installable-and-published, A2) ---------------------------------------------
# A house with receivers and no calibration captures yet runs on the physics of the current fit (solver/
# model_defaults.json, written by tools/make_model_defaults.py): each model's global terms and spread, one level per
# receiver kind. Nothing in it names a receiver, device, room or anchor.
DEFAULTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_defaults.json")
GLOBAL_EXTRAS = ("gup", "Dw")              # the ceiling-AP back lobe, the XIAO dipole weight: global, not per receiver


def load_defaults(path=DEFAULTS_PATH):
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path}: the default model (model_defaults.json) is missing")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def receiver_kind(name):
    """"echo" or "esp32", from the receiver's name without an epoch suffix (" (old2)")."""
    base = name.split(" (old")[0]
    return "echo" if is_echo(base) else "esp32"


OWN_TERMS = ("e:", "x:", "ac:", "as:", "ag:")    # a receiver's own fitted terms; 0 is "none"


def at_kind_levels(names, theta, levels):
    """A copy of a fit's theta with every receiver at its kind's level and without its own terms (vertical gain,
    exponent offset, azimuth lobe): the parameters a house on the defaults runs with, the physics kept."""
    out = np.array(theta, dtype=float)
    for i, n in enumerate(names):
        if n.startswith("K:"):
            out[i] = levels[receiver_kind(n[2:])]
        elif n.startswith(OWN_TERMS):
            out[i] = 0.0
    return out


def default_fit(model, knames, defaults):
    """A Fit from the defaults: the model's terms and extras, every name in knames (epochs too) at its kind's level,
    no anchors, the defaults' spread."""
    d = defaults["models"][model]
    missing = [k for k in USES[model] if k not in d["p"]]
    if missing:
        raise ValueError(f"model_defaults.json has no {', '.join(missing)} for {model} (the model's terms follow its "
                         f"flags): regenerate it with tools/make_model_defaults.py under the image's settings")
    extras = [k for k in GLOBAL_EXTRAS if d.get(k) is not None]
    names = list(USES[model]) + extras + ["K:" + s for s in knames]
    theta = np.array([float(d["p"][k]) for k in USES[model]] + [float(d[k]) for k in extras] +
                     [float(defaults["levels"][receiver_kind(s)]) for s in knames])
    F = Fit(model, list(knames), [], theta, names, [])
    F.mad, F.rms = float(d["mad"]), float(d.get("rms", d["mad"]))
    return F


FIT_LOSS = os.environ.get("RP_LOSS", "cauchy")   # one scanner can be 15 dB off from unmapped clutter; don't let it steer (flags.py: huber, linear to try)


class Problem:
    """The fit as an objective: residual rows (data over sigma, then the prior pseudo-observations), bounds, the
    prior's mean and sd, and pred(theta). Exposed so global_fit.py can attack the same function."""
    def __init__(self, names, mean, sd, lo, hi, res, pred):
        self.names, self.mean, self.sd, self.lo, self.hi, self.res, self.pred = names, mean, sd, lo, hi, res, pred


def objective(P, theta):
    """least_squares' cost at f_scale=1: 0.5 * sum(rho(r^2)) with rho for the configured loss."""
    r = P.res(theta); z = r * r
    if FIT_LOSS == "cauchy":
        rho = np.log1p(z)
    elif FIT_LOSS == "huber":
        rho = np.where(z <= 1, z, 2 * np.sqrt(z) - 1)
    elif FIT_LOSS == "soft_l1":
        rho = 2 * (np.sqrt(1 + z) - 1)
    else:
        rho = z
    return float(0.5 * np.sum(rho))


def warm_start(P):
    """Start vector: the prior mean, overridden by name from the JSON file RP_X0 points at ({"theta": {name: v}})."""
    x0 = np.clip(P.mean, P.lo + 1e-6, P.hi - 1e-6)
    path = os.environ.get("RP_X0")
    if path:
        th = json.load(open(path)).get("theta", {})
        for i, n in enumerate(P.names):
            if n in th:
                x0[i] = min(max(float(th[n]), P.lo[i] + 1e-6), P.hi[i] - 1e-6)
    return x0


def fit(model, y, kidx, oidx, farr, scanners, anchors):
    """Vectorised robust fit of problem(...) from warm_start(...)."""
    P = problem(model, y, kidx, oidx, farr, scanners, anchors)
    x0 = warm_start(P)
    sol = least_squares(P.res, x0, bounds=(P.lo, P.hi), loss=FIT_LOSS, f_scale=1.0, max_nfev=5000)
    F = Fit(model, scanners, anchors, sol.x, P.names, list(y - P.pred(sol.x)))
    F.x0 = x0
    return F


def problem(model, y, kidx, oidx, farr, scanners, anchors):
    """The fit's objective as a Problem.

    y      (R,) observed medians
    kidx   (R,) index of the scanner for each row
    oidx   (R,) index of the anchor for each row, -1 for the phone
    farr   dict of (R,) feature arrays
    """
    # Ceiling APs aim their antennas downward: scanners ABOVE a WAP sit in its back lobe.
    # One extra gain term for those links (training only; the phone never uses it).
    extra = ["gup"] if ("wap_up" in farr and np.any(farr["wap_up"])) else []
    evs = [s for s in scanners if is_echo(s)] if (VGAIN and not model.startswith("FreeSpace")) else []
    xs = list(scanners) if NRX else []
    azs = [s for s in scanners if is_echo(s)] if (AZGAIN and "ax" in farr and not model.startswith("FreeSpace")) else []
    sector = AZLOBE == "sector"
    per = 3 if sector else 2
    aznames = [p + s for s in azs for p in (("ag:", "az:", "aw:") if sector else ("ac:", "as:"))]
    azpri = [AZS_PRIOR["G"], AZS_PRIOR["c"], AZS_PRIOR["hw"]] * len(azs) if sector else [AZ_PRIOR] * (2 * len(azs))
    names = (["K:" + s for s in scanners] + ["o:" + a for a in anchors] + USES[model] + ["e:" + s for s in evs] + extra + ["x:" + s for s in xs]
             + aznames)
    nS, nA, nE, nX, nZ = len(scanners), len(anchors), len(evs), len(xs), per * len(azs)
    GUP = (0.0, 5.0, -20.0, 20.0)
    mean = np.array([K_PRIOR[0]] * nS + [O_PRIOR[0]] * nA + [PRIOR[k][0] for k in USES[model]] + [E_PRIOR[0]] * nE + [GUP[0]] * len(extra) + [X_PRIOR[0]] * nX + [q[0] for q in azpri])
    sd = np.array([K_PRIOR[1]] * nS + [O_PRIOR[1]] * nA + [PRIOR[k][1] for k in USES[model]] + [E_PRIOR[1]] * nE + [GUP[1]] * len(extra) + [X_PRIOR[1]] * nX + [q[1] for q in azpri])
    lo = np.array([K_PRIOR[2]] * nS + [O_PRIOR[2]] * nA + [PRIOR[k][2] for k in USES[model]] + [E_PRIOR[2]] * nE + [GUP[2]] * len(extra) + [X_PRIOR[2]] * nX + [q[2] for q in azpri])
    hi = np.array([K_PRIOR[3]] * nS + [O_PRIOR[3]] * nA + [PRIOR[k][3] for k in USES[model]] + [E_PRIOR[3]] * nE + [GUP[3]] * len(extra) + [X_PRIOR[3]] * nX + [q[3] for q in azpri])
    dip = DIPOLE and not model.startswith("FreeSpace")
    if dip:                                               # RP_DIPOLE: one weight on the dipole pattern, last in theta
        names = names + ["Dw"]
        mean, sd = np.append(mean, DIPOLE_PRIOR[0]), np.append(sd, DIPOLE_PRIOR[1])
        lo, hi = np.append(lo, DIPOLE_PRIOR[2]), np.append(hi, DIPOLE_PRIOR[3])
        gdip = dipole_db(farr) * np.array([1.0 if is_dipole(scanners[k]) else 0.0 for k in kidx])
    x0i = nS + nA + len(USES[model]) + nE + len(extra)   # first per-receiver exponent offset
    z0i = x0i + nX                                        # first azimuth pair
    zrow = np.array([azs.index(scanners[k]) if scanners[k] in azs else -1 for k in kidx], dtype=int)
    e0 = nS + nA + len(USES[model])                      # first per-Show vertical gain
    erow = np.array([evs.index(scanners[k]) if scanners[k] in evs else -1 for k in kidx], dtype=int)
    vr = vert(farr) if nE else None
    SIG = 3.0                                   # dB per data residual, before the robust loss
    sig_row = np.sqrt(SIG ** 2 + np.array([extra_sd(scanners[k]) for k in kidx]) ** 2)
    has_o = oidx >= 0

    def pred(theta):
        p = {k: theta[nS + nA + i] for i, k in enumerate(USES[model])}
        if nX:
            p["dnx"] = theta[x0i + kidx]
        v = mu(model, p, theta[kidx], farr)
        if nA:
            v = v + np.where(has_o, theta[nS + np.maximum(oidx, 0)], 0.0)
        if nE:
            v = v + np.where(erow >= 0, theta[e0 + np.maximum(erow, 0)], 0.0) * vr
        if extra:
            v = v + theta[x0i - 1] * farr["wap_up"]
        if nZ:
            j = np.maximum(zrow, 0)
            if sector:
                g = theta[z0i + 3 * j] * sector_weight(farr["ax"], farr["ay"], theta[z0i + 3 * j + 1], theta[z0i + 3 * j + 2])
            else:
                g = theta[z0i + 2 * j] * farr["ax"] + theta[z0i + 2 * j + 1] * farr["ay"]
            v = v + np.where(zrow >= 0, g, 0.0)
        if dip:
            v = v + theta[-1] * gdip
        return v

    def res(theta):
        return np.concatenate([(y - pred(theta)) / sig_row, (theta - mean) / sd])

    return Problem(names, mean, sd, lo, hi, res, pred)


# ---------------------------------------------------------------------------------
DETECT_FLOOR = -95.0     # medians below this are truncated by receiver sensitivity
CENSOR_AT = -93.0


def split_obs(obs, min_n):
    det, cen = {}, []
    for s, o in obs.items():
        if o["n"] >= min_n and o["med"] > DETECT_FLOOR:
            det[s] = o["med"]
        else:
            cen.append(s)
    return det, cen


NU = 2.0                 # Student-t dof for localisation: heavy enough that 6 honest scanners outvote 1 liar

# Fuzzy constants (fuzz_opt.py): how much to believe each kind of evidence. Defaults reproduce the
# deployed behaviour exactly; fuzz_opt.py overwrites them in-process while optimising.
FUZZ = dict(censor_at=CENSOR_AT, nu=NU, sigma_scale=SIGMA_SCALE, echo_sd=ECHO_EXTRA_SD, off_sd_mult=1.0,
            cen_w=1.0,            # weight on the non-detection (censored) terms
            weak_r0=-100.0,       # soft down-weight of weak links: w = sigmoid((rssi - r0) / weak_w)
            weak_w=5.0,
            sig_base=4.0)         # sigma floor before SIGMA_SCALE


def localise(model, F, obs, scanners, bank, meta, P, min_n=10, off_sd=4.0, sigma=None, nu=None, dead=()):
    """Posterior over candidate cells. obs: {scanner: {med,n}} (absent scanners = censored).

    dead: scanners that heard NOTHING from any device during the capture. Their silence is
    not evidence of distance, so they are dropped entirely rather than censored. (Treating a
    dead proxy as a non-detection quietly pushes every answer away from it.)
    """
    dead = set(dead or ())
    det, cen = split_obs(obs, min_n)
    det = {s: v for s, v in det.items() if s not in dead}
    cen = [s for s in cen if s not in dead] + [s for s in scanners if s not in obs and s not in dead]
    if model in ("Bermuda", "Bermuda+offsets"):
        return None
    # Experiment (topn_eval.py): localise on only the N strongest links. Weaker detections are dropped
    # entirely, never turned into censored terms; RP_TOPN_CEN=1 keeps the never-heard terms.
    if os.environ.get("RP_TOPN"):
        keep = sorted(det, key=lambda s_: -det[s_])[:int(os.environ["RP_TOPN"])]
        det = {s_: det[s_] for s_ in keep}
        if os.environ.get("RP_TOPN_CEN", "0") != "1":
            cen = []
    elif os.environ.get("RP_TOPN_CEN", "1") == "0":
        cen = []
    nu = nu or FUZZ["nu"]
    off_sd = off_sd * FUZZ["off_sd_mult"]
    # Calibration: on the current 9-scanner layout r68 contained 100% of beacon errors at err/r68
    # = 0.30 (DominantPath) - the model out-ran its own uncertainty. RP_SIGMA_SCALE tightens the
    # likelihood so r68, p_room and the CALL verdict track the accuracy actually achieved.
    sig = (sigma or max(FUZZ["sig_base"], 1.3 * F.mad)) * FUZZ["sigma_scale"]
    span = max(15.0, 2.5 * off_sd)
    O = np.arange(-span, span + 0.01, 0.5)
    tot = np.zeros((len(P), len(O)))
    for s in scanners:
        m = mu_s(model, F, s, bank[s])[:, None] + O[None, :]
        sg = float(np.sqrt(sig * sig + (FUZZ["echo_sd"] if is_echo(s) else 0.0) ** 2))
        if s in det:
            r = det[s] - m
            w = 1.0 / (1.0 + math.exp(-(det[s] - FUZZ["weak_r0"]) / FUZZ["weak_w"]))
            tot += w * (-(nu + 1) / 2 * np.log1p(r * r / (nu * sg * sg)))
        elif s in cen:
            tot += FUZZ["cen_w"] * log_ndtr((FUZZ["censor_at"] - m) / sg)
    tot += -0.5 * (O / off_sd) ** 2
    lp = logsumexp(tot, axis=1)
    g = _groups(meta, P)
    clp = np.array([logsumexp(lp[ix]) for ix in g["members"]])
    post = np.exp(clp - logsumexp(clp))
    return dict(post=post, xy=g["xy"], fi=g["fi"], room=g["room"], sigma=sig)


_GROUPS = {}


def _groups(meta, P):
    key = id(meta)
    if key not in _GROUPS:
        uniq = {}
        for i, m_ in enumerate(meta):
            uniq.setdefault((m_[0], m_[3], m_[4]), []).append(i)
        cells = list(uniq)
        first = [uniq[k][0] for k in cells]
        _GROUPS[key] = dict(members=[np.array(uniq[k]) for k in cells], xy=P[first, :2],
                            fi=np.array([k[0] for k in cells]), room=[meta[i][1] for i in first])
    return _GROUPS[key]


def summarise(R, house, conf_room=0.5, conf_floor=0.6):
    post, fi, xy, room = R["post"], R["fi"], R["xy"], R["room"]
    pf = np.array([post[fi == i].sum() for i in range(len(house.floors))])
    rooms = {}
    for p_, f_, r_ in zip(post, fi, room):
        rooms[(f_, r_)] = rooms.get((f_, r_), 0.0) + p_
    top = sorted(rooms.items(), key=lambda kv: -kv[1])
    k = int(np.argmax(post))
    order = np.argsort(-post); cum = np.cumsum(post[order])
    s68 = order[:int(np.searchsorted(cum, 0.68)) + 1]
    same = s68[fi[s68] == fi[k]]
    r68 = float(np.max(np.hypot(*(xy[same] - xy[k]).T))) if len(same) else 0.0
    (bf, broom), bp = top[0]
    verdict = "CALL" if (bp >= conf_room and pf[bf] >= conf_floor) else "LOW-CONF"
    return dict(floor=int(np.argmax(pf)), p_floor=float(pf.max()), room=broom, room_floor=int(bf),
                p_room=float(bp), top=top[:3], xy=tuple(xy[k]), map_floor=int(fi[k]), r68=r68,
                floors_in_68=sorted(set(int(x) for x in fi[s68])), verdict=verdict,
                wall_clear=house.wall_clearance(int(fi[k]), *xy[k]))
