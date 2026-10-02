// Placement geometry for the Calibrate tab. Pure functions, no DOM or three.js, so Node can test them.
//
// The wall raster is the SAME one the model uses (api/house voxels, 5 cm cells): row r spans
// y = ymax - (r+1)*res .. ymax - r*res, column c spans x = c*res .. (c+1)*res. Snapping and the wall
// dimension lines use interior (4), exterior (5) and glass (6) walls only.

export const WALL_CODES = [4, 5, 6];
export const STEP = { in: 0.0254, cm: 0.10 };

export function makeRaster(house, fi) {
  const f = house.floors[fi], W = f.W, H = f.H;
  const wall = new Uint8Array(W * H);
  const vox = house.voxels[fi] || {};
  for (const code of WALL_CODES) for (const k of vox[code] || vox[String(code)] || []) wall[k] = 1;
  return { W, H, res: house.res, ymax: f.ymax, x0: f.x0 || 0, wall };
}

function cellOf(R, x, y) {
  return { r: Math.floor((R.ymax - y) / R.res), c: Math.floor((x - (R.x0 || 0)) / R.res) };
}

function isWall(R, r, c) {
  return r >= 0 && r < R.H && c >= 0 && c < R.W && R.wall[r * R.W + c] === 1;
}

// Distance from (x, y) to the first wall face in each direction, or null if none before the edge.
export function wallDistances(R, x, y) {
  const none = { east: null, west: null, north: null, south: null };
  const { r, c } = cellOf(R, x, y);
  if (r < 0 || r >= R.H || c < 0 || c >= R.W || isWall(R, r, c)) return none;
  const out = { ...none };
  for (let k = c + 1; k < R.W; k++) if (isWall(R, r, k)) { const face = (R.x0 || 0) + k * R.res; out.east = { face, d: face - x }; break; }
  for (let k = c - 1; k >= 0; k--) if (isWall(R, r, k)) { const face = (R.x0 || 0) + (k + 1) * R.res; out.west = { face, d: x - face }; break; }
  for (let k = r - 1; k >= 0; k--) if (isWall(R, k, c)) { const face = R.ymax - (k + 1) * R.res; out.north = { face, d: face - y }; break; }
  for (let k = r + 1; k < R.H; k++) if (isWall(R, k, c)) { const face = R.ymax - k * R.res; out.south = { face, d: y - face }; break; }
  return out;
}

// The closer wall face along x (east/west) and along y (north/south).
export function nearestXY(R, x, y) {
  const d = wallDistances(R, x, y);
  const pick = (a, an, b, bn) => (!a && !b) ? null : (!b || (a && a.d <= b.d)) ? { side: an, ...a } : { side: bn, ...b };
  return { x: pick(d.east, 'east', d.west, 'west'), y: pick(d.north, 'north', d.south, 'south') };
}

// Within `radius` of a wall face along an axis, sit exactly `offset` off it. Both axes -> a corner.
export function snapPoint(R, x, y, offset, radius) {
  const n = nearestXY(R, x, y);
  const out = { x, y, snapped: { x: null, y: null } };
  if (n.x && n.x.d <= radius) {
    out.x = n.x.side === 'east' ? n.x.face - offset : n.x.face + offset;
    out.snapped.x = n.x.face;
  }
  if (n.y && n.y.d <= radius) {
    out.y = n.y.side === 'north' ? n.y.face - offset : n.y.face + offset;
    out.snapped.y = n.y.face;
  }
  return out;
}

export function nudge(p, dir, step) {
  const d = { N: [0, 1], S: [0, -1], E: [1, 0], W: [-1, 0] }[dir];
  return { x: p.x + d[0] * step, y: p.y + d[1] * step };
}

export function fmtLen(m, units) {
  if (units === 'cm') {
    const cm = Math.round(m * 1000) / 10;
    return (Number.isInteger(cm) ? cm.toFixed(0) : cm.toFixed(1)) + ' cm';
  }
  return (m / 0.0254).toFixed(1) + ' in';
}

// The floor the Calibrate tab opens on: the one it was on, kept to the floors the house has (a one-floor house has
// no floor 1 - A5 clean room).
export function startFloor(saved, nFloors) {
  const f = Number.isInteger(saved) ? saved : 0;
  return Math.max(0, Math.min(f, nFloors - 1));
}

// A calibration tag as a household knows it: the device's name, with the id its rounds use.
export function tagName(t, key) {
  if (!t) return key;
  return t.label && t.label !== t.id ? `${t.label} (${t.id})` : t.id;
}
