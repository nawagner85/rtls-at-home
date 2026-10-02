// True geometry in the viewer (spec 2026-09-26): wall columns drawn in bands at their windows' sills and heads,
// fixtures drawn from their z_min, and a 2-D plan of one floor with a north arrow. Reusable by the later dashboard
// cards: nothing here reads the page except the elements it is given.
import { wallColumnBands, planSymbols, northArrow, stairSymbol, wallRects, exteriorApertures, rectBox, planFrame } from './geometry_logic.js';

const VOX = { 4: { c: 0x94a3b8, wall: true }, 5: { c: 0x64748b, wall: true }, 6: { c: 0x7dd3fc, glass: true },
              7: { c: 0xa0522d }, 8: { c: 0x7d3c98 } };
const FIX = { low: 0xc8b89a, med: 0xb08968, high: 0x7a8fa6, water: 0x38bdf8, wood: 0xc8a97e, stone: 0x9aa5b1,
              metal_top: 0x5f6b7a, door: 0xa0522d, wall: 0x94a3b8, none: 0x999999 };
const PLAN = { wall: '#334155', exterior: '#1e293b', glass: '#7dd3fc', door: '#a0522d', fixture: '#94a3b8', room: '#e2e8f0',
               text: '#e2e8f0', north: '#f8fafc' };

// ---------- 3-D ----------
// Walls for one floor: cells of the same kind (plain interior, plain exterior, or one band list) merge into
// rectangles (wallRects) and each rectangle is one box per band - glass translucent, doors and openings their own
// band. Returns the meshes to add to the floor group.
export function wallMeshes(THREE, f, voxels, bands, res) {
  const out = [];
  const banded = new Map((bands || []).map(b => [b[0], { code: b[1], sill: b[2], head: b[3] }]));
  const exterior = new Set(voxels['5'] || []);
  const plain = (voxels['4'] || []).concat(voxels['5'] || []).filter(k => !banded.has(k));
  // pane: an aperture band is drawn as a pane inset 1 cm from its sill and head and 40% of the wall's thickness,
  // centred, so it never shares a face with the piers or the wall beside it
  const box = (rects, colour, from, to, opacity = 1, pane = false) => {
    if (!rects.length) return null;
    const f0 = pane ? from + 0.01 : from, t0 = pane ? to - 0.01 : to;
    const geo = new THREE.BoxGeometry(1, t0 - f0, 1); geo.translate(0.5, (t0 + f0) / 2, 0.5);   // unit box: x 0..1, z 0..1 (south)
    const mat = new THREE.MeshLambertMaterial({ color: colour, transparent: opacity < 1, opacity });
    const im = new THREE.InstancedMesh(geo, mat, rects.length);
    const m4 = new THREE.Matrix4();
    rects.forEach((q, i) => {                          // scale the unit box to the rectangle (rectBox, tested)
      const b = rectBox(q, res, f.ymax, { pane, x0: f.x0 || 0 });
      m4.makeScale(b.sx, 1, b.sz); m4.setPosition(b.x, 0, b.z); im.setMatrixAt(i, m4); });
    im.userData = { wall: true, band: { from, to } };
    return im;
  };
  const p1 = plain.filter(k => !exterior.has(k)), p2 = plain.filter(k => exterior.has(k));
  for (const [cells, colour] of [[p1, VOX[4].c], [p2, VOX[5].c]]) { const m = box(wallRects(cells, f.W), colour, 0, f.ceiling); if (m) out.push(m); }
  // band cells, grouped by identical band lists (with the wall code of the wall they sit in) so each group is one
  // instanced mesh per band
  const extAp = exteriorApertures([...banded.keys()], exterior, f.W);
  const groups = new Map();
  for (const [k, cell] of banded) {
    const key = JSON.stringify(wallColumnBands(cell, f.ceiling, extAp.has(k) ? 5 : 4));
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(k);
  }
  for (const [key, cells] of groups) {
    const rects = wallRects(cells, f.W);
    for (const b of JSON.parse(key)) {
      const spec = VOX[b.code] || VOX[4];
      const m = box(rects, spec.c, b.from, b.to, spec.glass ? 0.45 : 1, !spec.wall);
      if (m) out.push(m);
    }
  }
  return out;
}

// A fixture as an extruded footprint from z_min to its top.
export function fixtureMesh(THREE, x) {
  const sh = new THREE.Shape(x.points.map(p => new THREE.Vector2(p[0], p[1])));
  const depth = Math.max(0.01, x.height - (x.z_min || 0));
  const geo = new THREE.ExtrudeGeometry(sh, { depth, bevelEnabled: false }); geo.rotateX(-Math.PI / 2);
  geo.translate(0, x.z_min || 0, 0);
  const thin = ['wood', 'stone', 'metal_top'].includes(x.rf);
  const m = new THREE.Mesh(geo, new THREE.MeshLambertMaterial({ color: FIX[x.rf] || FIX.none, transparent: true, opacity: thin ? 0.9 : 0.45 }));
  m.userData = { fixture: x.id, rf: x.rf };
  return m;
}

// A flight as a sloped slab on its LOWER floor at true pitch (Nick 2026-09-26: break, don't stretch - exploded, the
// top hangs in the gap pointing at the well above). Side profile in the (run, z) plane: the ramp's top surface from
// (c0, z0) to (c1, z1) with a landing to the well's end, 0.25 m thick, extruded across the well.
export function rampMesh(THREE, r) {
  const T = 0.25, [c0, c1] = r.run;
  const lo = r.axis === 'x' ? r.x0 : r.y0, hi = r.axis === 'x' ? r.x1 : r.y1;
  const across = r.axis === 'x' ? [r.y0, r.y1] : [r.x0, r.x1];
  const prof = new THREE.Shape([[c0, r.z0], [c1, r.z1], [hi, r.z1], [hi, r.z1 - T], [c1, r.z1 - T], [c0, r.z0 - T]]
    .map(p => new THREE.Vector2(p[0], p[1])));
  const geo = new THREE.ExtrudeGeometry(prof, { depth: across[1] - across[0], bevelEnabled: false });
  // the profile is in (run, z); lay the extrusion across the well
  if (r.axis === 'x') { geo.translate(0, 0, across[0]); geo.scale(1, 1, -1); }           // z_scene = -y
  else { geo.rotateY(-Math.PI / 2); geo.translate(across[0], 0, 0); geo.scale(1, 1, 1); }
  const m = new THREE.Mesh(geo, new THREE.MeshLambertMaterial({ color: FIX.wood, transparent: true, opacity: 0.8, side: THREE.DoubleSide }));
  m.userData = { ramp: true, lower: r.lower };
  return m;
}

// The well opening outlined on the UPPER floor's slab, so the eye connects the hanging top of the flight to it.
export function wellOutline(THREE, r) {
  const pts = [[r.x0, r.y0], [r.x1, r.y0], [r.x1, r.y1], [r.x0, r.y1], [r.x0, r.y0]].map(p => new THREE.Vector3(p[0], 0.02, -p[1]));
  const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color: FIX.wood }));
  line.userData = { well: true, lower: r.lower };
  return line;
}

// ---------- 2-D plan ----------
// Draws one floor on a canvas: rooms with names, walls as bands, apertures as plan symbols, fixtures as outlines,
// a north arrow and the floor name. `markers` [{x, y, colour, label}] are drawn last (devices, proxies).
export function drawPlan(canvas, house, fi, { markers = [], hover = null } = {}) {
  const ctx = canvas.getContext('2d');
  const f = house.floors[fi], pad = 40, x0 = f.x0 || 0;
  const { s, X, Y } = planFrame(f, house.res, canvas.width, canvas.height, pad);
  if (canvas.width < 2 * pad + 10 || canvas.height < 2 * pad + 10) return { X: () => 0, Y: () => 0, s };   // not laid out yet
  ctx.fillStyle = '#0b0f15'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.lineJoin = 'round';
  for (const r of house.rooms.filter(r => r.floor === fi)) {
    ctx.beginPath(); r.points.forEach((p, i) => (i ? ctx.lineTo : ctx.moveTo).call(ctx, X(p[0]), Y(p[1]))); ctx.closePath();
    ctx.fillStyle = r.outdoor ? '#111827' : '#1f2937'; ctx.fill();
  }
  // walls from the raster cells (plain and banded alike are walls in plan)
  const vox = house.voxels[fi], res = house.res;
  for (const [code, colour] of [['4', PLAN.wall], ['5', PLAN.exterior]]) {
    ctx.fillStyle = colour;
    for (const k of vox[code] || []) { const r = Math.floor(k / f.W), c = k % f.W; ctx.fillRect(X(x0 + c * res), Y((f.ymax - r * res)), res * s + 0.5, res * s + 0.5); }
  }
  for (const sym of planSymbols(house.apertures[fi] || [])) {
    const [ax, ay] = sym.a, [bx, by] = sym.b;
    if (sym.kind === 'window') { ctx.strokeStyle = PLAN.glass; ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(X(ax), Y(ay)); ctx.lineTo(X(bx), Y(by)); ctx.stroke(); }
    else {
      ctx.strokeStyle = '#1f2937'; ctx.lineWidth = res * s + 2; ctx.beginPath(); ctx.moveTo(X(ax), Y(ay)); ctx.lineTo(X(bx), Y(by)); ctx.stroke();   // the gap
    }
  }
  for (const x of house.fixtures.filter(x => x.floor === fi && x.rf !== 'none')) {
    ctx.beginPath(); x.points.forEach((p, i) => (i ? ctx.lineTo : ctx.moveTo).call(ctx, X(p[0]), Y(p[1]))); ctx.closePath();
    ctx.strokeStyle = hover === x.id ? '#facc15' : PLAN.fixture; ctx.lineWidth = hover === x.id ? 2 : 1; ctx.stroke();
    if (hover === x.id) { const c = x.points.reduce((a, p) => [a[0] + p[0] / x.points.length, a[1] + p[1] / x.points.length], [0, 0]);
      ctx.fillStyle = '#facc15'; ctx.font = '11px sans-serif'; ctx.fillText(x.id, X(c[0]) + 4, Y(c[1]) - 4); }
  }
  // stairs: the flight on its lower floor (UP), the well on its upper floor (DN)
  for (const r of house.ramps || []) {
    if (r.lower !== fi && r.lower + 1 !== fi) continue;
    const sym = stairSymbol(r, { upper: r.lower + 1 === fi });
    ctx.strokeStyle = PLAN.fixture; ctx.lineWidth = 1;
    if (sym.well) { ctx.beginPath(); sym.well.forEach((p, i) => (i ? ctx.lineTo : ctx.moveTo).call(ctx, X(p[0]), Y(p[1]))); ctx.closePath(); ctx.stroke(); }
    for (const t of sym.treads) { ctx.beginPath(); ctx.moveTo(X(t.a[0]), Y(t.a[1])); ctx.lineTo(X(t.b[0]), Y(t.b[1])); ctx.stroke(); }
    const [ax, ay] = sym.arrow.a, [bx, by] = sym.arrow.b;
    ctx.strokeStyle = PLAN.text; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.moveTo(X(ax), Y(ay)); ctx.lineTo(X(bx), Y(by)); ctx.stroke();
    const ang = Math.atan2(Y(by) - Y(ay), X(bx) - X(ax));
    ctx.beginPath(); ctx.moveTo(X(bx), Y(by)); ctx.lineTo(X(bx) - 8 * Math.cos(ang - 0.4), Y(by) - 8 * Math.sin(ang - 0.4));
    ctx.moveTo(X(bx), Y(by)); ctx.lineTo(X(bx) - 8 * Math.cos(ang + 0.4), Y(by) - 8 * Math.sin(ang + 0.4)); ctx.stroke();
    ctx.fillStyle = PLAN.text; ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText(sym.label, X((ax + bx) / 2), Y((ay + by) / 2) - 4);
  }
  ctx.fillStyle = PLAN.text; ctx.font = 'bold 13px sans-serif'; ctx.textAlign = 'center';
  for (const r of house.rooms.filter(r => r.floor === fi)) ctx.fillText(r.name, X(r.centroid[0]), Y(r.centroid[1]));
  for (const m of markers) { ctx.fillStyle = m.colour || '#22c55e'; ctx.beginPath(); ctx.arc(X(m.x), Y(m.y), 5, 0, 2 * Math.PI); ctx.fill();
    if (m.label) { ctx.font = '11px sans-serif'; ctx.fillStyle = PLAN.text; ctx.fillText(m.label, X(m.x), Y(m.y) - 8); } }
  // orientation: floor name and the north arrow (the plan's north is up)
  const n = northArrow();
  // top-left (the top-left card is gone): the right edge sits under the side panel
  ctx.textAlign = 'left'; ctx.fillStyle = PLAN.north; ctx.font = 'bold 14px sans-serif'; ctx.fillText(f.name, 12, 24);
  const ox = 26, oy = 62;
  ctx.strokeStyle = PLAN.north; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(ox, oy + 18); ctx.lineTo(ox + n.dx * 18, oy + n.dy * 18); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(ox, oy - 18); ctx.lineTo(ox - 5, oy - 8); ctx.lineTo(ox + 5, oy - 8); ctx.closePath(); ctx.fillStyle = PLAN.north; ctx.fill();
  ctx.textAlign = 'center'; ctx.fillText(n.label, ox, oy + 34);
  return { X, Y, s };
}

// Which fixture is under a canvas point (for hover names); null if none.
export function fixtureAt(house, fi, xy, px, py) {
  const inside = (pts, x, y) => { let hit = false; for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [xi, yi] = pts[i], [xj, yj] = pts[j]; if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) hit = !hit; } return hit; };
  const x = (xy.x0 || 0) + (px - xy.pad) / xy.s, y = xy.H - (py - xy.pad) / xy.s;
  const f = house.fixtures.filter(f => f.floor === fi && f.rf !== 'none').find(f => inside(f.points, x, y));
  return f ? f.id : null;
}
