"""The map editor's server side (spec docs/specs/2026-09-30-map-builder-design.md, section 4): the draft house document,
its problems and 3-D preview, the floor underlay images, and the Home Assistant floors and areas the bridge reports.

The draft lives in the data folder (house.draft.json) and never touches the applied house.json (Apply is phase C).
house_payload() is the /api/house shape for any geom3d.House - the live one and a draft's preview alike.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "solver"))
import geom3d as G            # noqa: E402
import house_build as HB      # noqa: E402
import house_check as HC      # noqa: E402
import house_doc as HD        # noqa: E402
import house_receivers as HR  # noqa: E402
from sessionlog import jsonable  # noqa: E402

MAX_UNDERLAY = 15 * 2**20
IMAGE_TYPES = {"image/png": ("png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
               "image/jpeg": ("jpg", lambda b: b.startswith(b"\xff\xd8\xff")),
               "image/webp": ("webp", lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP")}
CONTENT_TYPE = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
FLOOR_ID = re.compile(r"[A-Za-z0-9_\-]{1,64}")
UNDERLAY = re.compile(r"underlays/([A-Za-z0-9_\-]{1,80})\.(png|jpg|webp)")


def _problems(doc, refs=None):
    try:
        return HC.check(doc, refs=refs)
    except Exception as e:                        # a document so broken the checks cannot run: say so, do not crash
        return [dict(code="doc", severity="error", message=f"cannot check this document ({type(e).__name__}: {e})",
                     floor=None, ids=[])]


def with_receivers(doc):
    """The editor's view of a house: one without receivers gets the named registry's (RP_REGISTRY) - what the engine
    reads for it - so the first apply after receiver placement (spec 2026-10-01) writes them into the house."""
    if doc.get("receivers"):
        return doc
    try:
        rx = HR.from_registry(doc, HR.registry_file(), skip_unknown=True)
    except Exception:                       # no registry, or one that does not fit this house: no receivers yet
        return doc
    return {**doc, "receivers": rx} if rx else doc


class HouseStore:
    def __init__(self, data_dir):
        self.dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.draft_path = os.path.join(data_dir, "house.draft.json")
        self.lock = threading.Lock()
        self._places = {"floors": [], "areas": [], "at": None}
        self.refs = {}       # {(floor id, room name): [what names it]}: the live engine's receivers and calibration

    # ---- the house documents ---------------------------------------------------------------------------------
    def applied(self):
        return with_receivers(HD.load())

    def has_draft(self):
        return os.path.exists(self.draft_path)

    def draft(self):
        with self.lock:
            if self.has_draft():
                with open(self.draft_path, encoding="utf-8") as f:
                    return with_receivers(json.load(f))      # a draft from before receivers gets them too
        return self.applied()

    def state(self):
        """What the editor opens with: the draft (the applied house when there is none), whether it is a draft, and
        its problems."""
        doc = self.draft()
        return dict(doc=doc, draft=self.has_draft(), problems=_problems(doc, self.refs))

    def save_draft(self, doc):
        """Saves any document that looks like a house (a dict with a schema and a floors list), problems and all -
        a draft may be unfinished. Returns at once: checking builds every floor (a second on the NUC, more on a Pi),
        so the editor asks for the problems separately (problems())."""
        if not isinstance(doc, dict) or "schema" not in doc or not isinstance(doc.get("floors"), list):
            raise ValueError("not a house document")
        with self.lock:
            HD.save(doc, self.draft_path)
        return dict(saved=True, hash=HD.doc_hash(doc)[:12])

    def problems(self):
        """The draft's problems and the hash of the document they are for."""
        doc = self.draft()
        return dict(hash=HD.doc_hash(doc)[:12], problems=_problems(doc, self.refs))

    def discard_draft_if(self, doc_hash):
        """Discards the draft only if it is still the document with this hash (an apply's), atomically against a save;
        True when it did."""
        with self.lock:
            try:
                with open(self.draft_path, encoding="utf-8") as f:
                    same = HD.doc_hash(json.load(f)) == doc_hash
            except (OSError, ValueError):
                return False
            if same:
                os.remove(self.draft_path)
            return same

    def discard_draft(self):
        with self.lock:
            if self.has_draft():
                os.remove(self.draft_path)

    # ---- underlays -------------------------------------------------------------------------------------------
    def save_underlay(self, floor_id, content_type, data):
        if not FLOOR_ID.fullmatch(str(floor_id or "")):
            raise ValueError("bad floor id")
        ct = (content_type or "").split(";")[0].strip().lower()
        if ct not in IMAGE_TYPES:
            raise ValueError("an underlay must be a PNG, JPEG or WebP image")
        ext, looks_right = IMAGE_TYPES[ct]
        if len(data) > MAX_UNDERLAY:
            raise ValueError(f"an underlay may be at most {MAX_UNDERLAY // 2**20} MB")
        if not looks_right(data):
            raise ValueError(f"that is not a {ext.upper()} image")
        # named by its content: a draft's new image never replaces (or shadows the repo's copy of) the image the
        # applied house shows; the same image uploaded again is the same file
        name = f"{floor_id}-{hashlib.sha256(data).hexdigest()[:8]}.{ext}"
        d = os.path.join(self.dir, "underlays")
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".part")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(d, name))
        return "underlays/" + name

    def underlay_file(self, name):
        """The file for an underlay name (underlays/<floor>.<ext>): the data folder's, else the repo's house/ copy."""
        if not UNDERLAY.fullmatch(str(name or "")):
            return None
        for base in (self.dir, os.path.dirname(HD.REPO_HOUSE)):
            p = os.path.join(base, name)
            if os.path.isfile(p):
                return p
        return None

    # ---- Home Assistant floors and areas (from the bridge) ---------------------------------------------------
    def set_places(self, p):
        try:
            floors = [dict(id=str(f["id"]), name=str(f["name"]), level=f.get("level"))
                      for f in p["floors"] if isinstance(f, dict) and f.get("id") and f.get("name")]
            areas = [dict(id=str(a["id"]), name=str(a["name"]), floor=a.get("floor"))
                     for a in p["areas"] if isinstance(a, dict) and a.get("id") and a.get("name")]
        except (KeyError, TypeError):
            return False
        if not isinstance(p.get("floors"), list) or not isinstance(p.get("areas"), list):
            return False
        self._places = {"floors": floors, "areas": areas, "at": time.time()}
        return True

    def places(self):
        return dict(self._places)


def house_payload(H, floor_names):
    """The /api/house geometry for a geom3d.House: floors, rooms, fixtures, wall voxels and aperture bands, apertures
    and stair ramps (the viewer's 3-D scene and 2-D plan draw from these)."""
    floors, rooms, fixtures, voxels, bands, apertures = [], [], [], [], [], []
    for fi, f in enumerate(H.floors):
        with open(os.path.join(H.build_dir, f["id"] + ".floorplan.json"), encoding="utf-8") as fh:
            fp = json.load(fh)
        h, w = f["rf"].shape
        floors.append(dict(index=fi, id=f["id"], name=floor_names[fi], z=f["z"], ceiling=f["ceil"],
                           ymax=f["ymax"], W=w, H=h, x0=H.X0, y0=H.Y0))
        for r in fp["rooms"]:
            pts = r["points"]
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            rooms.append(dict(floor=fi, id=r["id"], name=r["name"], kind=r.get("kind"), outdoor=bool(r.get("outdoor")),
                              points=pts, centroid=r.get("centroid") or [cx, cy]))
        for fx in fp.get("fixtures", []):
            fixtures.append(dict(floor=fi, id=fx["id"], rf=fx.get("rf"), height=float(fx.get("height_m") or 0),
                                 z_min=float(fx.get("z_min") or 0), points=fx["points"]))
        cells = {}
        for code in (4, 5, 6, 7, 8):
            rr, cc = (f["rf"] == code).nonzero()
            cells[str(code)] = (rr * w + cc).tolist()
        voxels.append(cells)
        # true geometry (spec 2026-09-26): per aperture cell its code, sill and head, so the viewer draws wall columns
        # in bands; apertures carry sill_m / head_m for the 2-D plan
        ac = f.get("ac")
        if ac is not None and ac.any():
            rr, cc = ac.nonzero()
            bands.append([[int(r * w + c), int(ac[r, c]), round(float(f["asill"][r, c]), 3), round(float(f["ahead"][r, c]), 3)]
                          for r, c in zip(rr, cc)])
        else:
            bands.append([])
        apertures.append([dict(id=a["id"], kind=a["kind"], material=a.get("material"), a=a["a"], b=a["b"],
                               sill=a.get("sill_m"), head=a.get("head_m")) for a in fp.get("apertures", [])])
    # the flights as ramps (2026-09-26): drawn on their lower floor at true pitch; 10 in treads along the run
    ramps = [dict(lower=r.lower, x0=r.x0, y0=r.y0, x1=r.x1, y1=r.y1, axis=r.run_axis,
                  run=[round(r.c_bottom, 3), round(r.c_top, 3)],
                  z0=round(r.z_bottom - float(H.Z[r.lower]), 3), z1=round(r.z_top - float(H.Z[r.lower]), 3),
                  treads=int(round(abs(r.c_top - r.c_bottom) / 0.254)))
             for r in getattr(H, "RAMPS", [])]
    return jsonable(dict(res=G.RES, floors=floors, rooms=rooms, fixtures=fixtures, voxels=voxels, bands=bands,
                         apertures=apertures, ramps=ramps))


def preview(doc, extras):
    """The /api/house payload of a (draft) document, built in a temporary folder, with its problems. A document the
    builder or the model cannot take gives {"error", "problems"} instead."""
    problems = _problems(doc)
    tmp = tempfile.mkdtemp(prefix="rtls_preview_")
    try:
        HB.build(doc, tmp)
        H = G.House(doc=doc, build_dir=tmp)
        out = house_payload(H, H.names_floor)
        out.update(extras or {})
        out["problems"] = problems
        return out
    except Exception as e:                          # BuildError, or a malformed document geom3d cannot read
        return {"error": f"{type(e).__name__}: {e}", "problems": problems}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
