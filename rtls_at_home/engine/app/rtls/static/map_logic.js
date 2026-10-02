// The map editor's logic (spec docs/specs/2026-09-30-map-builder-design.md, section 4): units, geometry, snapping,
// document edits and undo. Pure: no imports, no DOM. A doc is a house document (solver/house_doc.py); every edit
// returns a NEW document and leaves its argument alone. Coordinates are metres, x right, y up.

const IN = 0.0254, FT = 0.3048;
const clone = (o) => structuredClone(o);
const r6 = (v) => Math.round(v * 1e6) / 1e6;
const pt = (p) => [r6(p[0]), r6(p[1])];

// ---------- units ----------
// Feet and inches (12'6", 12' 6", 12', 150", 6", 0'6", 6½") or metric (3.8 m, 3.8m, 380 cm, 38 mm, 3.8 = metres).
export function parseLength(text) {
  if (text == null) return null;
  const s = String(text).trim().toLowerCase().replace(/[’′]/g, "'").replace(/[”″]/g, '"').replace(/''/g, '"');
  if (!s) return null;
  let m = s.match(/^(\d+(?:\.\d+)?)\s*(?:'|ft|feet)\s*(?:(\d+(?:\.\d+)?)?\s*(½)?\s*(?:"|in)?)?$/);
  if (m) return pos(+m[1] * FT + ((m[2] ? +m[2] : 0) + (m[3] ? 0.5 : 0)) * IN);
  m = s.match(/^(\d+(?:\.\d+)?)?\s*(½)?\s*(?:"|in)$/);
  if (m && (m[1] || m[2])) return pos(((m[1] ? +m[1] : 0) + (m[2] ? 0.5 : 0)) * IN);
  m = s.match(/^(\d+(?:\.\d+)?)\s*(m|cm|mm)?$/);
  if (m) return pos(+m[1] * { m: 1, cm: 0.01, mm: 0.001 }[m[2] || 'm']);
  return null;
}
const pos = (v) => (Number.isFinite(v) && v > 0 ? r6(v) : null);

export function parseSize(text) {
  const parts = String(text ?? '').split(/\s*(?:x|×|by)\s*/i);
  if (parts.length !== 2) return null;
  const a = parseLength(parts[0]), b = parseLength(parts[1]);
  return a == null || b == null ? null : [a, b];
}

// A height off the floor, a sill or a room's step: a length that may be 0, and negative when signed.
export function parseOffset(text, { signed = false } = {}) {
  const s = String(text ?? '').trim();
  if (!s) return null;
  const neg = s.startsWith('-');
  if (neg && !signed) return null;
  const body = s.replace(/^[-+]\s*/, '');
  if (/^0+(?:\.0*)?\s*(?:m|cm|mm|'|ft|"|in)?$/i.test(body)) return 0;
  const v = parseLength(body);
  return v == null ? null : neg ? -v : v;
}

export function fmtLength(m, units = 'ft-in') {
  if (m == null || !Number.isFinite(m)) return '';
  if (units === 'm') return `${m.toFixed(2)} m`;
  const halves = Math.round((m / IN) * 2), inches = halves / 2;
  const ft = Math.floor(inches / 12), rest = inches - ft * 12, whole = Math.floor(rest);
  return `${ft}' ${whole}${rest - whole >= 0.5 ? '½' : ''}"`;
}

// ---------- geometry ----------
export function rectOutline(p, q) {
  const x0 = Math.min(p[0], q[0]), x1 = Math.max(p[0], q[0]), y0 = Math.min(p[1], q[1]), y1 = Math.max(p[1], q[1]);
  return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map(pt);
}

export function polyArea(pts) {
  let a = 0;
  for (let i = 0; i < pts.length; i++) { const [x0, y0] = pts[i], [x1, y1] = pts[(i + 1) % pts.length]; a += x0 * y1 - x1 * y0; }
  return Math.abs(a) / 2;
}

export function pointInPoly(p, pts) {
  const [x, y] = p;
  let hit = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [xi, yi] = pts[i], [xj, yj] = pts[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) hit = !hit;
  }
  return hit;
}

export function bbox(pts) {
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  return { x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys) };
}

export function centroid(pts) {
  let a = 0, cx = 0, cy = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x0, y0] = pts[i], [x1, y1] = pts[(i + 1) % pts.length], c = x0 * y1 - x1 * y0;
    a += c; cx += (x0 + x1) * c; cy += (y0 + y1) * c;
  }
  if (Math.abs(a) < 1e-12) return [pts.reduce((s, p) => s + p[0], 0) / pts.length, pts.reduce((s, p) => s + p[1], 0) / pts.length];
  return [cx / (3 * a), cy / (3 * a)];
}

export function segProject(p, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1], L2 = dx * dx + dy * dy;
  const t = L2 === 0 ? 0 : Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2));
  const q = [a[0] + t * dx, a[1] + t * dy];
  return { t, point: q, dist: Math.hypot(p[0] - q[0], p[1] - q[1]) };
}

export const edgesOf = (outline) => outline.map((p, i) => [p, outline[(i + 1) % outline.length]]);

// The room edge nearest p on floor fi within maxDist (ties: the earlier room), or null.
export function nearestEdge(doc, fi, p, maxDist) {
  let best = null;
  for (const r of doc.floors[fi].rooms) {
    edgesOf(r.outline).forEach(([a, b], i) => {
      const s = segProject(p, a, b);
      if (s.dist <= maxDist && (!best || s.dist < best.dist - 1e-12)) best = { room: r.id, i, a, b, t: s.t, dist: r6(s.dist), point: pt(s.point) };
    });
  }
  return best;
}

// The longest stretch where an edge of room A and an edge of room B lie on one line (within tol), or null.
export function sharedSegment(roomA, roomB, tol = 0.02) {
  let best = null;
  for (const [a0, a1] of edgesOf(roomA.outline)) {
    const L = Math.hypot(a1[0] - a0[0], a1[1] - a0[1]);
    if (L < 1e-9) continue;
    const ux = (a1[0] - a0[0]) / L, uy = (a1[1] - a0[1]) / L;
    for (const [b0, b1] of edgesOf(roomB.outline)) {
      const off = (q) => Math.abs((q[0] - a0[0]) * uy - (q[1] - a0[1]) * ux);
      if (off(b0) > tol || off(b1) > tol) continue;
      const tb0 = (b0[0] - a0[0]) * ux + (b0[1] - a0[1]) * uy, tb1 = (b1[0] - a0[0]) * ux + (b1[1] - a0[1]) * uy;
      const lo = Math.max(0, Math.min(tb0, tb1)), hi = Math.min(L, Math.max(tb0, tb1));
      if (hi - lo > tol && (!best || hi - lo > best.len)) {
        best = { a: pt([a0[0] + ux * lo, a0[1] + uy * lo]), b: pt([a0[0] + ux * hi, a0[1] + uy * hi]), len: hi - lo };
      }
    }
  }
  return best ? { a: best.a, b: best.b } : null;
}

// Every room edge of a floor, split where it runs along an edge of another indoor room: those parts are interior
// walls, the rest exterior. An outdoor room's edges are neither (outdoor: true). [{room, a, b, exterior, outdoor}]
export function wallEdges(floor, tol = 0.03) {
  const out = [], indoor = floor.rooms.filter((r) => r.kind !== 'outdoor');
  for (const r of floor.rooms) {
    for (const [a, b] of edgesOf(r.outline)) {
      const L = Math.hypot(b[0] - a[0], b[1] - a[1]);
      if (L < 1e-9) continue;
      if (r.kind === 'outdoor') { out.push({ room: r.id, a, b, exterior: false, outdoor: true }); continue; }
      const ux = (b[0] - a[0]) / L, uy = (b[1] - a[1]) / L, off = (q) => Math.abs((q[0] - a[0]) * uy - (q[1] - a[1]) * ux);
      const cover = [];
      for (const o of indoor) {
        if (o === r) continue;
        for (const [c, e] of edgesOf(o.outline)) {
          if (off(c) > tol || off(e) > tol) continue;
          const tc = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy, te = (e[0] - a[0]) * ux + (e[1] - a[1]) * uy;
          const lo = Math.max(0, Math.min(tc, te)), hi = Math.min(L, Math.max(tc, te));
          if (hi - lo > tol) cover.push([lo, hi]);
        }
      }
      const at = (t) => pt([a[0] + ux * t, a[1] + uy * t]);
      const seg = (t0, t1, exterior) => { if (t1 - t0 > 1e-6) out.push({ room: r.id, a: at(t0), b: at(t1), exterior, outdoor: false }); };
      let t = 0;
      for (const [lo, hi] of cover.sort((p, q) => p[0] - q[0])) {
        if (hi <= t) continue;
        seg(t, lo, true); seg(Math.max(t, lo), hi, false); t = hi;
      }
      seg(t, L, true);
    }
  }
  return out;
}

// What a click at p picks on floor fi: the smallest object under it (or within tol/2 of its edge), else the nearest
// aperture or open wall within tol, else the smallest room under it; null for nothing. Returns a ref.
export function hitTest(doc, fi, p, tol) {
  const f = doc.floors[fi], byArea = (u, v) => polyArea(u.outline) - polyArea(v.outline);
  const o = (f.objects || []).filter((x) => pointInPoly(p, x.outline) ||
    edgesOf(x.outline).some(([a, b]) => segProject(p, a, b).dist <= tol / 2)).sort(byArea)[0];
  if (o) return refOf('object', fi, o.id);
  let best = null;
  const near = (kind, id, a, b) => { const d = segProject(p, a, b).dist; if (d <= tol && (!best || d < best.d)) best = { d, ref: refOf(kind, fi, id) }; };
  (f.apertures || []).forEach((a) => near('aperture', a.id, a.a, a.b));
  if (best) return best.ref;
  (f.open_edges || []).forEach((e, i) => near('open', e.id ?? i, e.a, e.b));
  if (best) return best.ref;
  const r = f.rooms.filter((x) => pointInPoly(p, x.outline)).sort(byArea)[0];
  return r ? refOf('room', fi, r.id) : null;
}

export const isRect = (o) => o.length === 4 && edgesOf(o).every(([a, b]) => Math.abs(a[0] - b[0]) < 1e-9 || Math.abs(a[1] - b[1]) < 1e-9);

// A rectangle to w x d with its lower-left corner fixed and its corner order kept.
export function setRectSize(outline, w, d) {
  const b = bbox(outline);
  return outline.map(([x, y]) => pt([Math.abs(x - b.x0) < 1e-9 ? b.x0 : b.x0 + w, Math.abs(y - b.y0) < 1e-9 ? b.y0 : b.y0 + d]));
}

// Edge i (from vertex i to i+1) to length len: a rectangle moves its far side with it; a polygon moves vertex i+1.
export function setEdgeLength(outline, i, len) {
  const o = outline.map((p) => p.slice()), n = o.length, a = o[i], b = o[(i + 1) % n];
  const L = Math.hypot(b[0] - a[0], b[1] - a[1]);
  if (!(len > 0) || L < 1e-9) return o;
  const nb = [a[0] + ((b[0] - a[0]) / L) * len, a[1] + ((b[1] - a[1]) / L) * len], dx = nb[0] - b[0], dy = nb[1] - b[1];
  o[(i + 1) % n] = pt(nb);
  if (isRect(outline)) { const c = o[(i + 2) % n]; o[(i + 2) % n] = pt([c[0] + dx, c[1] + dy]); }
  return o;
}

export const moveOutline = (pts, dx, dy) => pts.map((p) => pt([p[0] + dx, p[1] + dy]));

// ---------- snapping ----------
// ctx = { grid: metres | 0, vertices: [[x, y]], edges: [[a, b]], from: [x, y] | null, ortho, tol }: a vertex within
// tol beats an edge within tol beats the grid; with ortho and a start point, the closer axis is locked first.
export function snap(p, ctx) {
  let q = p.slice();
  if (ctx.ortho && ctx.from) { if (Math.abs(q[0] - ctx.from[0]) >= Math.abs(q[1] - ctx.from[1])) q[1] = ctx.from[1]; else q[0] = ctx.from[0]; }
  const tol = ctx.tol ?? 0.15;
  let bv = null;
  for (const v of ctx.vertices || []) { const d = Math.hypot(v[0] - q[0], v[1] - q[1]); if (d <= tol && (!bv || d < bv.d)) bv = { v, d }; }
  if (bv) return { p: pt(bv.v), kind: 'vertex' };
  let be = null;
  for (const [a, b] of ctx.edges || []) { const s = segProject(q, a, b); if (s.dist <= tol && (!be || s.dist < be.dist)) be = s; }
  if (be) return { p: pt(be.point), kind: 'edge' };
  if (ctx.align) {                                  // line up with a corner's x and/or y; the grid takes the rest
    let ax = null, ay = null;
    for (const v of ctx.vertices || []) {
      if (Math.abs(v[0] - q[0]) <= tol && (ax == null || Math.abs(v[0] - q[0]) < Math.abs(ax - q[0]))) ax = v[0];
      if (Math.abs(v[1] - q[1]) <= tol && (ay == null || Math.abs(v[1] - q[1]) < Math.abs(ay - q[1]))) ay = v[1];
    }
    const g = (v) => (ctx.grid ? r6(Math.round(v / ctx.grid) * ctx.grid) : r6(v));
    if (ax != null || ay != null) return { p: [ax != null ? r6(ax) : g(q[0]), ay != null ? r6(ay) : g(q[1])], kind: 'align' };
  }
  if (ctx.grid) return { p: [r6(Math.round(q[0] / ctx.grid) * ctx.grid), r6(Math.round(q[1] / ctx.grid) * ctx.grid)], kind: 'grid' };
  return { p: pt(q), kind: 'none' };
}

// ---------- document edits ----------
const slug = (s) => String(s || 'item').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '') || 'item';
// counted: the prefix ends in a counter ('room3'), which counts on; otherwise 2, 3, ... are appended ('door2', and
// 'stairs_floor1_2' so an id never reads as another floor's).
function uniqueId(prefix, taken, counted = false) {
  const t = new Set(taken);
  if (!t.has(prefix)) return prefix;
  const m = counted && prefix.match(/^(.*?)(\d+)$/), base = m ? m[1] : /\d$/.test(prefix) ? prefix + '_' : prefix;
  for (let n = m ? +m[2] + 1 : 2; ; n++) if (!t.has(base + n)) return base + n;
}
function uniqueName(word, n, taken) {
  const t = new Set(taken);
  for (let k = n; ; k++) if (!t.has(`${word} ${k}`)) return `${word} ${k}`;
}
const allIds = (f) => [...f.rooms, ...(f.objects || []), ...(f.apertures || []), ...(f.open_edges || [])].map((x) => x.id).filter(Boolean);
const roomAt = (f, p) => (f.rooms.find((r) => pointInPoly(p, r.outline)) || null);

export const APERTURE_WIDTH = { door: 0.81, window: 0.91, garage_door: 4.88, opening: 0.91 };

// What a suggestion means to the model: material and construction (house_doc.object_class), height and height off
// the floor (Nick 2026-09-30: "what to call it, where it is and how big it is, and whether we should care").
export const PRESETS = {
  'Fridge': { material: 'metal', construction: 'solid', height: 1.8, z_min: 0 },
  'Kitchen island': { material: 'wood', construction: 'solid', height: 0.91, z_min: 0 },
  'Cabinets': { material: 'wood', construction: 'solid', height: 0.9, z_min: 0 },
  'Upper cabinets': { material: 'wood', construction: 'solid', height: 0.75, z_min: 1.4 },
  'Counter top': { material: 'stone', construction: 'top', height: 0.04, z_min: 0.88 },
  'Bookshelf': { material: 'wood', construction: 'solid', height: 1.8, z_min: 0 },
  'Bed': { material: 'wood', construction: 'solid', height: 0.6, z_min: 0 },
  'Sofa': { material: 'light', construction: 'solid', height: 0.85, z_min: 0 },
  'Table': { material: 'wood', construction: 'top', height: 0.04, z_min: 0.72 },
  'Desk': { material: 'wood', construction: 'top', height: 0.04, z_min: 0.72 },
  'Dresser': { material: 'wood', construction: 'solid', height: 0.8, z_min: 0 },
  'Mirror': { material: 'metal', construction: 'panel', height: 1.2, z_min: 0.9 },
  'TV': { material: 'metal', construction: 'panel', height: 0.7, z_min: 0.6 },
  'Aquarium': { material: 'water', construction: 'solid', height: 0.5, z_min: 0.8 },
  'Water heater': { material: 'metal', construction: 'solid', height: 1.5, z_min: 0 },
  'Washer/dryer': { material: 'metal', construction: 'solid', height: 0.9, z_min: 0 },
  'Car': { material: 'metal', construction: 'solid', height: 1.5, z_min: 0 },
  'Shower glass': { material: 'glass', construction: 'panel', height: 2.0, z_min: 0 },
  'Half wall': { material: 'wood', construction: 'solid', height: 1.0, z_min: 0 },
  'Other': { material: 'none', construction: 'solid', height: 1.0, z_min: 0 },
};

export const refOf = (kind, fi, id) => ({ kind, fi, id });

// Moves a room with what belongs to it: its objects, the doors and windows on its walls (unless they are also on the
// wall of a room that stays put) and the run of stairs that start in it.
export function moveRoom(doc, fi, id, dx, dy) {
  const d = clone(doc), f = d.floors[fi], r = f.rooms.find((x) => x.id === id);
  if (!r) return doc;
  const mv = (p) => pt([p[0] + dx, p[1] + dy]);
  const onWall = (p, room) => edgesOf(room.outline).some(([a, b]) => segProject(p, a, b).dist < 0.02);
  for (const a of f.apertures || []) {
    const mid = [(a.a[0] + a.b[0]) / 2, (a.a[1] + a.b[1]) / 2];
    if (onWall(mid, r) && !f.rooms.some((o) => o !== r && onWall(mid, o))) { a.a = mv(a.a); a.b = mv(a.b); }
  }
  for (const o of f.objects || []) if (o.room === id) o.outline = moveOutline(o.outline, dx, dy);
  for (const st of d.stairs || []) {
    if (st.lower.floor !== f.id || st.lower.room !== id) continue;
    if (st.foot) st.foot = mv(st.foot);
    if (st.top) st.top = mv(st.top);
    if (st.run) { const k = st.run.axis === 'x' ? dx : dy; st.run.from = r6(st.run.from + k); st.run.to = r6(st.run.to + k); }
  }
  r.outline = moveOutline(r.outline, dx, dy);
  return d;
}

export function addRoom(doc, fi, outline, kind = 'room', name = null) {
  const d = clone(doc), f = d.floors[fi];
  const id = kind === 'stairs' ? uniqueId('stairs', allIds(f)) : uniqueId('room' + (f.rooms.length + 1), allIds(f), true);
  name = name || (kind === 'stairs' ? 'Stairs' : uniqueName('Room', f.rooms.length + 1, f.rooms.map((r) => r.name)));
  f.rooms.push({ id, name, kind, outline: outline.map(pt) });
  return d;
}

export function addObject(doc, fi, outline, presetName = 'Other') {
  const d = clone(doc), f = d.floors[fi], pre = PRESETS[presetName] || PRESETS.Other;
  f.objects = f.objects || [];
  const r = roomAt(f, centroid(outline)) || f.rooms[0];
  const height = Math.min(pre.height, Math.max(0.01, f.ceiling - pre.z_min));
  f.objects.push({ id: uniqueId(slug(presetName), allIds(f)), name: presetName, room: r ? r.id : null, outline: outline.map(pt),
                   z_min: pre.z_min, height: r6(height), material: pre.material, construction: pre.construction });
  return d;
}

export function addAperture(doc, fi, kind, hit, width = null) {
  if (!hit) return doc;
  const d = clone(doc), f = d.floors[fi], [A, B] = [hit.a, hit.b];
  const L = Math.hypot(B[0] - A[0], B[1] - A[1]);
  if (L < 1e-6) return doc;
  const w = Math.min(width || APERTURE_WIDTH[kind] || 0.9, L), ux = (B[0] - A[0]) / L, uy = (B[1] - A[1]) / L;
  const s = Math.max(w / 2, Math.min(L - w / 2, hit.t * L));
  f.apertures = f.apertures || [];
  f.apertures.push({ id: uniqueId(kind, allIds(f)), kind, a: pt([A[0] + ux * (s - w / 2), A[1] + uy * (s - w / 2)]),
                     b: pt([A[0] + ux * (s + w / 2), A[1] + uy * (s + w / 2)]) });
  return d;
}

// The straight wall at p (within maxDist): the stretches of room edges (wallEdges) that lie on one line end to end and
// are, like the stretch under p, all outside walls or all inside walls. Rooms are logical (a kitchen and a dining room
// on one outside wall), so an outside wall runs past the rooms behind it and stops where it turns, ends, or becomes an
// inside wall (Nick 2026-10-01). {a, b} oriented like the stretch under p, or null.
export function wallRun(doc, fi, p, maxDist, tol = 0.03) {
  const pieces = wallEdges(doc.floors[fi]).filter((w) => !w.outdoor);
  let best = null;
  for (const w of pieces) {
    const s = segProject(p, w.a, w.b);
    if (s.dist <= maxDist && (!best || s.dist < best.s.dist - 1e-12)) best = { w, s };
  }
  if (!best) return null;
  const { a, b } = best.w, len = Math.hypot(b[0] - a[0], b[1] - a[1]), ux = (b[0] - a[0]) / len, uy = (b[1] - a[1]) / len;
  const off = (q) => Math.abs((q[0] - a[0]) * uy - (q[1] - a[1]) * ux), along = (q) => (q[0] - a[0]) * ux + (q[1] - a[1]) * uy;
  const iv = pieces.filter((w) => w.exterior === best.w.exterior && off(w.a) <= tol && off(w.b) <= tol)
    .map((w) => [Math.min(along(w.a), along(w.b)), Math.max(along(w.a), along(w.b))]).sort((x, y) => x[0] - y[0]);
  const runs = [];
  for (const [l, h] of iv) {
    const last = runs[runs.length - 1];
    if (last && l <= last[1] + tol) last[1] = Math.max(last[1], h); else runs.push([l, h]);
  }
  const tm = along(best.s.point), [lo, hi] = runs.find(([l, h]) => tm >= l - tol && tm <= h + tol) || [0, len];
  return { a: pt([a[0] + ux * lo, a[1] + uy * lo]), b: pt([a[0] + ux * hi, a[1] + uy * hi]) };
}

// The wall an aperture sits on (the straight wall at its middle, wallRun) and where along it: offset from the wall's
// start to the aperture's near end, its width, the wall's length. null when it is on no wall.
export function apertureSpan(doc, fi, ap) {
  const e = wallRun(doc, fi, [(ap.a[0] + ap.b[0]) / 2, (ap.a[1] + ap.b[1]) / 2], 0.1);
  if (!e) return null;
  const wall = Math.hypot(e.b[0] - e.a[0], e.b[1] - e.a[1]), ux = (e.b[0] - e.a[0]) / wall, uy = (e.b[1] - e.a[1]) / wall;
  const ta = (ap.a[0] - e.a[0]) * ux + (ap.a[1] - e.a[1]) * uy, tb = (ap.b[0] - e.a[0]) * ux + (ap.b[1] - e.a[1]) * uy;
  return { edge: e, wall: r6(wall), offset: r6(Math.min(ta, tb)), width: r6(Math.abs(tb - ta)) };
}

// Moves or resizes an aperture along its wall, kept on the wall.
export function setApertureSpan(doc, ref, { offset = null, width = null } = {}) {
  const ap = itemOf(doc, ref), s = ap && apertureSpan(doc, ref.fi, ap);
  if (!s) return doc;
  const w = Math.min(width ?? s.width, s.wall), o = Math.max(0, Math.min(s.wall - w, offset ?? s.offset));
  const e = s.edge, ux = (e.b[0] - e.a[0]) / s.wall, uy = (e.b[1] - e.a[1]) / s.wall;
  return updateItem(doc, ref, { a: pt([e.a[0] + ux * o, e.a[1] + uy * o]), b: pt([e.a[0] + ux * (o + w), e.a[1] + uy * (o + w)]) });
}

export function addOpenEdge(doc, fi, roomA, roomB) {
  const d = clone(doc), f = d.floors[fi];
  const ra = f.rooms.find((r) => r.id === roomA), rb = f.rooms.find((r) => r.id === roomB);
  const seg = ra && rb ? sharedSegment(ra, rb) : null;
  if (!seg) return doc;
  f.open_edges = f.open_edges || [];
  f.open_edges.push({ id: uniqueId('open', allIds(f)), a: seg.a, b: seg.b, rooms: [roomA, roomB] });
  return d;
}

// Ids for open walls that have none (the converted house's), so a selection names one and not a position in the list.
// Returns the same document when every open wall has one.
export function ensureIds(doc) {
  if (doc.floors.every((f) => (f.open_edges || []).every((e) => e.id))) return doc;
  const d = clone(doc);
  for (const f of d.floors) for (const e of f.open_edges || []) if (!e.id) e.id = uniqueId('open', allIds(f));
  return d;
}

// Drags an aperture along its wall: mode 'end' moves the end that was at `from` to p (the other end stays; never
// thinner than 10 cm), 'body' slides it by how far p is along the wall from `from`. Kept on the wall.
export function dragAperture(doc, ref, mode, p, from) {
  const ap = itemOf(doc, ref), s = ap && apertureSpan(doc, ref.fi, ap);
  if (!s) return doc;
  const e = s.edge, ux = (e.b[0] - e.a[0]) / s.wall, uy = (e.b[1] - e.a[1]) / s.wall;
  const t = (q) => Math.max(0, Math.min(s.wall, (q[0] - e.a[0]) * ux + (q[1] - e.a[1]) * uy));
  if (mode === 'body') return setApertureSpan(doc, ref, { offset: s.offset + t(p) - t(from) });
  const lo = s.offset, hi = s.offset + s.width, tf = t(from), tp = t(p);
  const other = Math.abs(tf - lo) <= Math.abs(tf - hi) ? hi : lo;
  if (tp < other) { const o = Math.min(tp, other - 0.1); return setApertureSpan(doc, ref, { offset: o, width: other - o }); }
  return setApertureSpan(doc, ref, { offset: other, width: Math.max(0.1, tp - other) });
}

export function addWall(doc, fi, a, b, thickness = 0.1) {
  const L = Math.hypot(b[0] - a[0], b[1] - a[1]);
  if (L < 0.05) return doc;
  const d = clone(doc), f = d.floors[fi], nx = (-(b[1] - a[1]) / L) * (thickness / 2), ny = ((b[0] - a[0]) / L) * (thickness / 2);
  const outline = [[a[0] + nx, a[1] + ny], [b[0] + nx, b[1] + ny], [b[0] - nx, b[1] - ny], [a[0] - nx, a[1] - ny]].map(pt);
  const r = roomAt(f, [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]) || f.rooms[0];
  f.objects = f.objects || [];
  f.objects.push({ id: uniqueId('wall', allIds(f)), name: 'Wall', room: r ? r.id : null, outline, z_min: 0, height: f.ceiling,
                   material: 'drywall', construction: 'panel' });
  return d;
}

const newFloor = (d, ref, elevation) => {
  const n = d.floors.length + 1, id = uniqueId('floor' + n, d.floors.map((f) => f.id), true);
  return { id, name: uniqueName('Floor', n, d.floors.map((f) => f.name)), elevation: r6(elevation), ceiling: ref.ceiling, slab: ref.slab, rooms: [], open_edges: [],
           apertures: [], objects: [] };
};

// A floor above or below floor `at`; everything from the inserted level up moves up by the new floor's height.
export function addFloor(doc, where, at) {
  const d = clone(doc), ref = d.floors[at];
  if (where === 'above') {
    const nf = newFloor(d, ref, ref.elevation + ref.ceiling + ref.slab), lift = nf.ceiling + nf.slab;
    d.floors.forEach((f, i) => { if (i > at) f.elevation = r6(f.elevation + lift); });
    d.floors.splice(at + 1, 0, nf);
  } else {
    const nf = newFloor(d, ref, ref.elevation), lift = nf.ceiling + nf.slab;
    d.floors.forEach((f, i) => { if (i >= at) f.elevation = r6(f.elevation + lift); });
    d.floors.splice(at, 0, nf);
  }
  return d;
}

// Floor fi one place up (dir 1) or down (dir -1) the stack; the elevations are stacked again from the bottom floor's,
// each floor its ceiling and slab above the one below. Stairs that no longer join a floor to the next one up show in
// the problems list. Returns the same document when there is no room to move.
export function moveFloor(doc, fi, dir) {
  const j = fi + dir;
  if (j < 0 || j >= doc.floors.length || j === fi) return doc;
  const d = clone(doc);
  let z = d.floors[0].elevation;
  [d.floors[fi], d.floors[j]] = [d.floors[j], d.floors[fi]];
  for (const f of d.floors) { f.elevation = r6(z); z += f.ceiling + f.slab; }
  return d;
}

// A stairs room on floor fi, the matching stairs room on the floor above (made if none is there) and the record:
// the run along dir across the outline, foot and top on its centre line just inside the ends.
export function addStairs(doc, fi, outline, dir = '+x') {
  if (fi + 1 >= doc.floors.length) throw new Error('Add a floor above first: stairs climb to the next floor up.');
  let d = addRoom(doc, fi, outline, 'stairs', 'Stairs up');
  const lower = d.floors[fi].rooms.at(-1), c = centroid(outline), up = d.floors[fi + 1];
  let upper = up.rooms.find((r) => r.kind === 'stairs' && pointInPoly(c, r.outline));
  if (!upper) { d = addRoom(d, fi + 1, outline, 'stairs', 'Stairs down'); upper = d.floors[fi + 1].rooms.at(-1); }
  d.stairs = d.stairs || [];
  d.stairs.push({ id: uniqueId('stairs_' + d.floors[fi].id, d.stairs.map((s) => s.id)),
                  lower: { floor: d.floors[fi].id, room: lower.id }, upper: { floor: up.id, room: upper.id },
                  risers: null, ...stairsRun(outline, dir) });
  return d;
}

// The run of stairs across a room outline along dir (+x, -x, +y, -y): foot and top on its centre line just inside
// the ends.
function stairsRun(outline, dir) {
  const bb = bbox(outline), axis = dir[1], sign = dir[0] === '-' ? -1 : 1;
  const lo = axis === 'x' ? bb.x0 : bb.y0, hi = axis === 'x' ? bb.x1 : bb.y1, from = sign > 0 ? lo : hi, to = sign > 0 ? hi : lo;
  const inset = Math.min(0.15, Math.abs(hi - lo) / 4), mid = axis === 'x' ? (bb.y0 + bb.y1) / 2 : (bb.x0 + bb.x1) / 2;
  const at = (v) => (axis === 'x' ? pt([v, mid]) : pt([mid, v]));
  return { run: { axis, from: r6(from), to: r6(to) }, foot: at(from + sign * inset), top: at(to - sign * inset) };
}

export const stairsDir = (st) => (st.run.to >= st.run.from ? '+' : '-') + st.run.axis;

// Turns stairs to climb along dir across their lower room.
export function setStairsDir(doc, id, dir) {
  const d = clone(doc), st = (d.stairs || []).find((s) => s.id === id);
  const f = st && d.floors.find((g) => g.id === st.lower.floor), room = f && f.rooms.find((r) => r.id === st.lower.room);
  if (!room) return doc;
  Object.assign(st, stairsRun(room.outline, dir));
  return d;
}

// Removes an element and what depends on it: a room takes its objects, open edges and stairs; a floor takes its
// contents and the stairs touching it, and the floors above close the gap.
export function deleteItem(doc, ref) {
  const d = clone(doc), f = d.floors[ref.fi];
  if (ref.kind === 'floor') {
    const gone = d.floors[ref.fi], drop = gone.ceiling + gone.slab;
    d.floors.forEach((g, i) => { if (i > ref.fi) g.elevation = r6(g.elevation - drop); });
    d.floors.splice(ref.fi, 1);
    d.stairs = (d.stairs || []).filter((s) => s.lower.floor !== gone.id && s.upper.floor !== gone.id);
    d.receivers = (d.receivers || []).filter((r) => r.floor !== gone.id);     // the editor refuses live ones first
    return d;
  }
  if (ref.kind === 'room') {
    f.rooms = f.rooms.filter((r) => r.id !== ref.id);
    f.objects = (f.objects || []).filter((o) => o.room !== ref.id);
    f.open_edges = (f.open_edges || []).filter((e) => !(e.rooms || []).includes(ref.id));
    for (const r of d.receivers || []) {           // roomless: the problems list then asks to move them onto the map
      if (r.floor === f.id && r.room === ref.id) r.room = null;
      for (const m of r.moves || []) if ((m.floor ?? r.floor) === f.id && m.room === ref.id) m.room = null;
    }
    d.stairs = (d.stairs || []).filter((s) => !((s.lower.floor === f.id && s.lower.room === ref.id) ||
                                                (s.upper.floor === f.id && s.upper.room === ref.id)));
  } else if (ref.kind === 'object') f.objects = f.objects.filter((o) => o.id !== ref.id);
  else if (ref.kind === 'aperture') f.apertures = f.apertures.filter((a) => a.id !== ref.id);
  else if (ref.kind === 'open') f.open_edges = f.open_edges.filter((e, i) => (typeof ref.id === 'number' ? i : e.id) !== ref.id);
  else if (ref.kind === 'stairs') d.stairs = d.stairs.filter((s) => s.id !== ref.id);
  return d;
}

export function itemOf(doc, ref) {
  if (ref.kind === 'receiver') return (doc.receivers || []).find((r) => r.id === ref.id) || null;
  const f = doc.floors[ref.fi];
  if (!f && ref.kind !== 'stairs') return null;
  if (ref.kind === 'floor') return f;
  if (ref.kind === 'room') return f.rooms.find((r) => r.id === ref.id) || null;
  if (ref.kind === 'object') return (f.objects || []).find((o) => o.id === ref.id) || null;
  if (ref.kind === 'aperture') return (f.apertures || []).find((a) => a.id === ref.id) || null;
  if (ref.kind === 'open') return (typeof ref.id === 'number' ? (f.open_edges || [])[ref.id] : (f.open_edges || []).find((e) => e.id === ref.id)) || null;
  if (ref.kind === 'stairs') return (doc.stairs || []).find((s) => s.id === ref.id) || null;
  return null;
}

export function updateItem(doc, ref, patch) {
  const d = clone(doc), it = itemOf(d, ref);
  if (it) Object.assign(it, clone(patch));
  return d;
}

// ---------- receivers (spec docs/specs/2026-10-01-receiver-placement-design.md) ----------
// Heights above the floor (the registry's class table, scanners_v2.json _heights_from).
export const HEIGHT_PRESETS = { 'Standard outlet': 0.3, 'Chest-height outlet': 1.22, 'Nightstand': 0.65, 'Cabinet top': 1.0 };
const RX_WALL = 0.25;                      // a proxy on an outlet is recorded at the wall line, outside its room

// The room a point is in, else the room whose wall line it is on (within RX_WALL); null when neither.
export function roomOf(floor, p) {
  const inside = floor.rooms.find((r) => pointInPoly(p, r.outline));
  if (inside) return inside;
  let best = null;
  for (const r of floor.rooms) for (const [a, b] of edgesOf(r.outline)) {
    const d = segProject(p, a, b).dist;
    if (d <= RX_WALL && (!best || d < best.d)) best = { d, r };
  }
  return best ? best.r : null;
}

const rxIndex = (doc, id) => (doc.receivers || []).findIndex((r) => r.id === id);
const samePlace = (a, b) => a.floor === b.floor && Math.abs(a.x - b.x) < 1e-9 && Math.abs(a.y - b.y) < 1e-9 &&
  Math.abs(a.height - b.height) < 1e-9;

const moveKey = (m) => [m.at, m.floor ?? '', m.x, m.y, m.height].join('|');
const pendingIndex = (r, applied) => {
  const live = new Set(((applied && applied.moves) || []).map(moveKey));
  for (let i = (r.moves || []).length - 1; i >= 0; i--) if (!live.has(moveKey(r.moves[i]))) return i;
  return -1;
};

// The move this draft added - one the live house's receiver does not have - or null. `applied` null: a receiver
// placed in this draft, which records no moves.
export function pendingMove(r, applied) {
  if (!applied) return null;
  const i = pendingIndex(r, applied);
  return i < 0 ? null : r.moves[i];
}

export function pendingEpochs(r, applied) {
  const have = new Set((applied && applied.epochs) || []);
  return (r.epochs || []).filter((e) => !have.has(e));
}

export function addReceiver(doc, fi, p, { name, mac = null, address = null, model = null, height = 0.3, added = null } = {}) {
  const d = clone(doc), f = d.floors[fi], room = roomOf(f, p);
  d.receivers = d.receivers || [];
  const id = uniqueId(slug(name || 'receiver'), d.receivers.map((r) => r.id));
  const r = { id, name: name || id, floor: f.id, room: room ? room.id : null, x: pt(p)[0], y: pt(p)[1], height: r6(height),
              epochs: [], moves: [] };
  for (const [k, v] of Object.entries({ mac, address, model, added })) if (v) r[k] = v;
  d.receivers.push(r);
  return d;
}

// Moves a receiver to p on floor fi (and to `height` when given). Against `applied` - the live house's receiver, null
// for one placed in this draft - the first change records a move (where it stood until `nowIso`), later ones keep
// it, and moving back removes it.
export function moveReceiver(doc, id, fi, p, applied, nowIso, height = null) {
  const i = rxIndex(doc, id);
  if (i < 0 || applied === undefined) return doc;          // undefined: the live house is not known (yet)
  const d = clone(doc), r = d.receivers[i], f = d.floors[fi], room = roomOf(f, p);
  Object.assign(r, { floor: f.id, room: room ? room.id : null, x: pt(p)[0], y: pt(p)[1], height: r6(height ?? r.height) });
  if (applied) {
    const pending = pendingMove(r, applied);
    if (samePlace(r, applied)) r.moves = clone(applied.moves || []);
    else if (!pending) {
      r.moves = [...(r.moves || []), { at: nowIso, floor: applied.floor, room: applied.room, x: applied.x, y: applied.y,
                                       height: applied.height }];
    }
  }
  return d;
}

// When the pending move really happened (captures between then and the apply are featurised at the new spot).
export function setMoveTime(doc, id, iso, applied) {
  const i = rxIndex(doc, id);
  if (i < 0 || !pendingMove(doc.receivers[i], applied)) return doc;
  const d = clone(doc);
  d.receivers[i].moves[pendingIndex(d.receivers[i], applied)].at = iso;
  return d;
}

export function addEpoch(doc, id, iso) {
  const i = rxIndex(doc, id);
  if (i < 0) return doc;
  const d = clone(doc), r = d.receivers[i];
  r.epochs = [...new Set([...(r.epochs || []), iso])].sort();
  return d;
}

// When a pending epoch really happened (a proxy re-seated yesterday); live ones stay.
export function setEpochTime(doc, id, oldIso, newIso, applied) {
  const i = rxIndex(doc, id);
  if (i < 0 || !pendingEpochs(doc.receivers[i], applied).includes(oldIso)) return doc;
  const d = clone(doc), r = d.receivers[i];
  r.epochs = [...new Set(r.epochs.map((e) => (e === oldIso ? newIso : e)))].sort();
  return d;
}

// Only an epoch this draft added: the fit depends on the live ones.
export function removeEpoch(doc, id, iso, applied) {
  const i = rxIndex(doc, id);
  if (i < 0 || !pendingEpochs(doc.receivers[i], applied).includes(iso)) return doc;
  const d = clone(doc);
  d.receivers[i].epochs = d.receivers[i].epochs.filter((e) => e !== iso);
  return d;
}

// Put back (spec 2026-10-01-placement-advisor section 4): a receiver returns to where the live house has it - floor,
// room, spot, height and moves - and its pending move goes; epochs marked in the draft stay. Not for a receiver placed
// in this draft (applied null) or when the live house is not known (undefined): the document is returned as it is.
export function putBack(doc, id, applied) {
  const i = rxIndex(doc, id);
  if (i < 0 || !applied) return doc;
  const d = clone(doc), r = d.receivers[i];
  for (const k of ['floor', 'room', 'x', 'y', 'height']) r[k] = applied[k];
  r.moves = clone(applied.moves || []);
  return d;
}

// Only a receiver this draft placed: a live one's captures need its history (switch it off on the Receivers tab).
export function removeReceiver(doc, id, applied) {
  if (applied !== null || rxIndex(doc, id) < 0) return doc;   // live, or not known whether it is live
  const d = clone(doc);
  d.receivers = d.receivers.filter((r) => r.id !== id);
  return d;
}

// The measured outlet (the outlet survey: floor index, x, y, z_rel) nearest p on floor fi within tol; the height is
// the outlet's plus `antenna` (the survey's antenna offset: where a proxy plugged in there hears from, as the
// placement advisor places it).
export function snapToOutlet(p, fi, outlets, tol = 0.3, antenna = 0) {
  let best = null;
  for (const o of outlets || []) {
    if (o.floor !== fi || o.x == null || o.y == null) continue;
    const d = Math.hypot(o.x - p[0], o.y - p[1]);
    if (d <= tol && (!best || d < best.d)) best = { d, o };
  }
  return best ? { p: [best.o.x, best.o.y], height: r6(best.o.z_rel + antenna), outlet: best.o } : null;
}

export const receiversOnFloor = (doc, fid) => (doc.receivers || []).filter((r) => r.floor === fid);

export function receiverAt(doc, fi, p, tol) {
  const fid = doc.floors[fi].id;
  let best = null;
  for (const r of doc.receivers || []) {
    if (r.floor !== fid) continue;
    const d = Math.hypot(r.x - p[0], r.y - p[1]);
    if (d <= tol && (!best || d < best.d)) best = { d, id: r.id };
  }
  return best ? best.id : null;
}

// The placement advisor's layout (spec 2026-10-01-placement-advisor) applied to a draft: each proxy assigned an outlet
// is moved there (a pending move against the live house, stamped nowIso), each outlet without one gets a new proxy
// ("New proxy (<room>)"), a proxy staying on its own spot or not needed is left alone. Heights are the advisor's
// (the outlet plus the antenna offset). Floors are found by id (a floor added in the draft shifts the indices); a
// proxy or a floor the draft no longer has is listed in `missing`.
export function layoutToDraft(draft, applied, result, nowIso) {
  let d = draft;
  const moved = [], added = [], missing = [];
  for (const m of (result && result.moves) || []) {
    if (m.receiver && String(m.candidate).startsWith('now:')) continue;
    const fi = m.floor_id != null ? d.floors.findIndex((f) => f.id === m.floor_id) : (d.floors[m.floor] ? m.floor : -1);
    if (fi < 0) { missing.push(m.receiver || m.candidate); continue; }
    if (m.receiver) {
      const r = (d.receivers || []).find((x) => x.name === m.receiver);
      if (!r) { missing.push(m.receiver); continue; }
      const live = ((applied && applied.receivers) || []).find((x) => x.id === r.id) || null;
      d = moveReceiver(d, r.id, fi, [m.x, m.y], live, nowIso, m.height);
      moved.push(m.receiver);
    } else {
      const names = new Set((d.receivers || []).map((x) => x.name)), base = `New proxy (${m.room || d.floors[fi].name})`;
      let name = base;
      for (let i = 2; names.has(name); i++) name = `${base} ${i}`;
      d = addReceiver(d, fi, [m.x, m.y], { name, height: m.height });
      added.push(name);
    }
  }
  return { doc: d === draft ? clone(draft) : d, moved, added, missing };
}

// "2026-10-01T09:05-07:00": local time with its offset, to the minute - how the registry writes its stamps.
export function isoLocal(d = new Date()) {
  const z = (n) => String(n).padStart(2, '0'), off = -d.getTimezoneOffset();
  return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}T${z(d.getHours())}:${z(d.getMinutes())}` +
    `${off >= 0 ? '+' : '-'}${z(Math.floor(Math.abs(off) / 60))}:${z(Math.abs(off) % 60)}`;
}

// ---------- the page ----------
// Escapes text for the page, attributes included: a feet-and-inches length has both quote marks in it.
export const escHtml = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// The rasters' origin moved out (to a whole cell) when a room reaches more than `slack` past it: the build cuts off
// anything west or south of the origin. Within the slack are the converted house's traced walls, whose maps are
// verified as they are. Returns the same document when nothing changes.
export function withFrame(doc, slack = 0.1) {
  const pts = doc.floors.flatMap((f) => f.rooms.flatMap((r) => r.outline));
  const fr = doc.frame || { x0: 0, y0: 0, res: 0.05 }, res = fr.res || 0.05;
  if (!pts.length) return doc;
  const mx = Math.min(...pts.map((p) => p[0])), my = Math.min(...pts.map((p) => p[1]));
  const x0 = mx < fr.x0 - slack ? r6(Math.floor(mx / res + 1e-9) * res) : fr.x0;
  const y0 = my < fr.y0 - slack ? r6(Math.floor(my / res + 1e-9) * res) : fr.y0;
  if (x0 === fr.x0 && y0 === fr.y0) return doc;
  const d = clone(doc);
  d.frame = { ...fr, x0, y0 };
  return d;
}

// An uploaded image on the floor it was uploaded for (by id: the floor shown may have changed while it uploaded),
// keeping the layout the floor's image already had; a first image is laid across the floor's rooms, or across
// `view` ({cx, cy, w} in metres) on an empty floor. size: the image's [width, height] in pixels. null when the floor
// is gone.
export function setUnderlay(doc, floorId, file, size, view) {
  const d = clone(doc), f = d.floors.find((g) => g.id === floorId);
  if (!f) return null;
  if (f.underlay) { f.underlay.file = file; return d; }
  const pts = f.rooms.flatMap((r) => r.outline), b = pts.length ? bbox(pts) : null;
  const w = b ? Math.max(1, b.x1 - b.x0) : view.w, mpp = w / size[0];
  f.underlay = { file, m_per_px: r6(mpp), x: r6(b ? b.x0 : view.cx - w / 2), y: r6(b ? b.y1 : view.cy + (mpp * size[1]) / 2),
                 rotation: 0, opacity: 0.5 };
  return d;
}

// The draft's autosave. Edits go out `delay` ms after the last one, one save at a time (an edit made while a save is
// out goes in the next); a failed save is retried with backoff; reset() - a reload or a discard - waits for a save
// still out, then drops its reply. send() returns a promise of the server's reply; onSaved(reply) gets each current
// one. schedule/cancel are setTimeout/clearTimeout (the tests pass a fake clock).
export function createSaver({ send, onSaved = () => {}, onState = () => {}, schedule, cancel, delay = 700,
                              backoff = [2000, 5000, 15000, 30000] }) {
  let ver = 0, saved = 0, gen = 0, timer = null, inflight = null, again = false, error = null, tries = 0;
  const state = () => ({ dirty: saved !== ver, saving: !!inflight, error });
  const arm = (ms) => { if (timer) cancel(timer); timer = schedule(flush, ms); };
  function flush() {
    if (timer) { cancel(timer); timer = null; }
    if (inflight) { again = true; return inflight; }
    if (saved === ver) return Promise.resolve();
    const v = ver, g = gen;
    const p = Promise.resolve().then(send).then((reply) => {
      if (g !== gen) return;
      saved = v; error = null; tries = 0;
      onSaved(reply);
    }, (e) => {
      if (g !== gen) return;
      error = (e && e.message) || String(e);
      arm(backoff[Math.min(tries++, backoff.length - 1)]);
    }).then(() => {
      if (inflight === p) inflight = null;
      if (g === gen && again) { again = false; if (!error) flush(); }
      onState(state());
    });
    inflight = p;
    onState(state());
    return p;
  }
  return {
    changed() { ver++; arm(delay); onState(state()); },
    flush,
    async reset() {
      gen++; again = false; error = null; tries = 0; ver = saved = 0;
      if (timer) { cancel(timer); timer = null; }
      const p = inflight;
      onState(state());
      if (p) await p;
    },
    get state() { return state(); },
  };
}

// ---------- undo ----------
export function history(limit = 100) {
  const back = [], fwd = [];
  return {
    push(doc) { back.push(clone(doc)); if (back.length > limit) back.shift(); fwd.length = 0; },
    undo(cur) { if (!back.length) return null; fwd.push(clone(cur)); return back.pop(); },
    redo(cur) { if (!fwd.length) return null; back.push(clone(cur)); return fwd.pop(); },
    get canUndo() { return back.length > 0; },
    get canRedo() { return fwd.length > 0; },
  };
}

// What Apply says. In setup mode nothing is tracked yet, so it does not promise a pause in tracking (A3 final review).
export function applyCopy(setup) {
  return setup
    ? { confirm: 'Apply now?', title: 'Put the draft live. The engine restarts on the new house.',
        wait: 'The engine restarts on the new house.' }
    : { confirm: 'Apply now? Tracking pauses ~1 min',
        title: 'Put the draft live. Tracking pauses for about a minute while the engine restarts.',
        wait: 'Tracking pauses for about a minute while the engine restarts.' };
}
