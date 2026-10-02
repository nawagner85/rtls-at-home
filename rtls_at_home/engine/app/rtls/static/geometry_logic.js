// True-geometry drawing, the pure parts (spec 2026-09-26): a wall column's bands from its aperture's sill and head,
// the 2-D plan symbols, the north arrow. No DOM, no three.js - tested by app/tests/geometry_logic.test.mjs.
const WALL = 4;

// A wall cell with an aperture band {code, sill, head} is wall below the sill, the aperture between sill and head,
// wall above the head. A plain wall (code 0, no band) is one band to the ceiling. The wall parts carry the cell's
// own wall code (4 interior, 5 exterior) so the piers between windows are painted like the wall they belong to.
export function wallColumnBands(cell, ceiling, wallCode = WALL) {
  const code = cell.code || 0;
  if (!code) return [{ from: 0, to: ceiling, code: wallCode }];
  const sill = Math.max(0, cell.sill ?? 0), head = Math.min(ceiling, cell.head ?? ceiling);
  const out = [];
  if (sill > 0) out.push({ from: 0, to: sill, code: wallCode });
  if (head > sill) out.push({ from: sill, to: head, code });
  if (head < ceiling) out.push({ from: head, to: ceiling, code: wallCode });
  return out;
}

// Raster cells (row-major keys r * W + c) merged into rectangles {r, c, w, h}: runs of consecutive columns in a row,
// then a run absorbs the identical run in the row below. One box per rectangle instead of one per cell: a wall face
// is a single face, not hundreds of coplanar 5 cm faces fighting in the depth buffer.
export function wallRects(cells, W) {
  const sorted = [...cells].sort((a, b) => a - b);
  const runs = [];                                   // per row, in column order
  for (const k of sorted) {
    const r = Math.floor(k / W), c = k % W, last = runs[runs.length - 1];
    if (last && last.r === r && last.c + last.w === c) last.w++;
    else runs.push({ r, c, w: 1, h: 1 });
  }
  const out = [], open = new Map();                  // "c,w" -> the rectangle still growing down from the previous row
  let row = -1, next = new Map();
  for (const run of runs) {
    if (run.r !== row) {                             // a new row: rectangles the previous row did not continue are done
      for (const [key, rect] of open) if (!next.has(key)) out.push(rect);
      open.clear(); for (const [key, rect] of next) open.set(key, rect); next.clear();
      if (run.r !== row + 1) { for (const rect of open.values()) out.push(rect); open.clear(); }
      row = run.r;
    }
    const key = run.c + ',' + run.w, above = open.get(key);
    if (above) { above.h++; next.set(key, above); open.delete(key); } else next.set(key, run);
  }
  for (const rect of open.values()) out.push(rect);
  for (const rect of next.values()) out.push(rect);
  return out.sort((a, b) => a.r - b.r || a.c - b.c);
}

// Plan symbols per aperture: a window is a thin line, a door or an opening a gap (no swings - Nick 2026-09-26).
export function planSymbols(apertures) {
  return apertures.map(a => ({ kind: a.kind, a: a.a, b: a.b, id: a.id }));
}

// The plan's north is the office's north wall: up the page.
export const northArrow = () => ({ dx: 0, dy: -1, label: 'N' });

// Stairs (Nick 2026-09-26). A flight is a ramp {x0, y0, x1, y1: the well; axis; run: [bottom, top] along the axis;
// z0, z1 above the lower floor; treads}. The plan symbol: one line across the well at each riser past the first,
// an arrow along the run's centre line (bottom -> top, 'UP' on the lower floor; reversed, 'DN', on the upper floor
// with the well outline).
export function stairSymbol(ramp, { upper = false } = {}) {
  const [c0, c1] = ramp.run, n = Math.max(1, ramp.treads | 0), step = (c1 - c0) / n;
  const across = ramp.axis === 'x' ? [ramp.y0, ramp.y1] : [ramp.x0, ramp.x1];
  const mid = (across[0] + across[1]) / 2;
  const pt = (c, t) => ramp.axis === 'x' ? [c, t] : [t, c];
  const treads = [];
  for (let i = 1; i <= n; i++) { const c = c0 + step * i; treads.push({ a: pt(c, across[0]), b: pt(c, across[1]) }); }
  const arrow = upper ? { a: pt(c1, mid), b: pt(c0, mid) } : { a: pt(c0, mid), b: pt(c1, mid) };
  const out = { treads, arrow, label: upper ? 'DN' : 'UP' };
  if (upper) out.well = [[ramp.x0, ramp.y0], [ramp.x1, ramp.y0], [ramp.x1, ramp.y1], [ramp.x0, ramp.y1]];
  return out;
}

// Which aperture cells sit in an exterior wall: the aperture cells carry the glass/door code, not the wall's, so
// group them into connected apertures (4-neighbours) and call an aperture exterior when any of its cells touches a
// plain exterior wall cell. Returns the set of exterior aperture cell keys.
export function exteriorApertures(bandKeys, exteriorKeys, W) {
  const cells = new Set(bandKeys), seen = new Set(), out = new Set();
  const nb = (k) => [k - 1, k + 1, k - W, k + W];
  for (const start of cells) {
    if (seen.has(start)) continue;
    const comp = [], stack = [start]; seen.add(start);
    let ext = false;
    while (stack.length) {
      const k = stack.pop(); comp.push(k);
      for (const n of nb(k)) {
        if (exteriorKeys.has(n)) ext = true;
        if (cells.has(n) && !seen.has(n)) { seen.add(n); stack.push(n); }
      }
    }
    if (ext) comp.forEach(k => out.add(k));
  }
  return out;
}

// A merged rectangle's box in scene coordinates (x east, z = -y): its north-west cell corner is at (c * res,
// y = ymax - r * res), it spans w cells east and h cells south (+z). A pane keeps 40% of the wall's thickness (the
// short side), centred, so glass never shares a face with the wall around it.
export function rectBox(q, res, ymax, { pane = false, x0 = 0 } = {}) {
  let x = x0 + q.c * res, z = -(ymax - q.r * res), sx = q.w * res, sz = q.h * res;
  if (pane && q.w !== q.h) {
    if (q.w > q.h) { const t = sz * 0.4; z += (sz - t) / 2; sz = t; } else { const t = sx * 0.4; x += (sx - t) / 2; sx = t; }
  }
  return { x, z, sx, sz };
}

// House as data (2026-09-30): any number of floors and a frame origin (x0) that need not be 0. Row 0 of a floor's
// raster is its north edge (y = ymax); column 0 starts at x0.
const PALETTE = [0x3b82f6, 0x10b981, 0xf59e0b, 0xec4899, 0x8b5cf6, 0xef4444];
export const floorColor = (i) => PALETTE[((i % PALETTE.length) + PALETTE.length) % PALETTE.length];
const RES = 0.05;
export const cellToXY = (f, r, c) => ({ x: (f.x0 || 0) + c * RES + RES / 2, y: f.ymax - (r * RES + RES / 2) });
export const xyToCell = (f, x, y) => ({ r: Math.floor((f.ymax - y) / RES), c: Math.floor((x - (f.x0 || 0)) / RES) });
// The plan view's mapping for one floor: scaled to fit the raster (W x H cells from x0 and down from ymax) inside the
// canvas with `pad` around it. Scaling on ymax alone clipped a floor reaching below y = 0 (final review 2026-09-30).
export function planFrame(f, res, width, height, pad) {
  const W = f.W * res, D = (f.H || 0) * res || f.ymax, x0 = f.x0 || 0;
  const s = Math.max(1e-6, Math.min((width - 2 * pad) / W, (height - 2 * pad) / D));
  return { s, X: (x) => pad + (x - x0) * s, Y: (y) => pad + (f.ymax - y) * s };
}
export function houseCentre(house) {
  const fs = house.floors || [];
  const x0 = Math.min(...fs.map(f => f.x0 || 0)), x1 = Math.max(...fs.map(f => (f.x0 || 0) + f.W * RES));
  const y0 = Math.min(...fs.map(f => f.y0 || 0)), y1 = Math.max(...fs.map(f => f.ymax));
  return { x: (x0 + x1) / 2, y: (y0 + y1) / 2 };
}
