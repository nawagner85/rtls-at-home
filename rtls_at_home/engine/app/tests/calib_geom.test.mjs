// node --test app/tests/
import test from 'node:test';
import assert from 'node:assert/strict';
import { makeRaster, wallDistances, nearestXY, snapPoint, nudge, fmtLen, STEP } from '../rtls/static/calib_geom.js';

// 5 x 4 m floor at 5 cm: an interior wall (code 4) in column 80 (x 4.00-4.05 m) and an exterior wall
// (code 5) in row 20 (y 2.95-3.00 m, since row r spans y = ymax - (r+1)*res .. ymax - r*res).
// A glass cell (code 6) and a door threshold (code 7, not a wall for snapping) elsewhere.
function house() {
  const W = 100, H = 80, v4 = [], v5 = [];
  for (let r = 0; r < H; r++) v4.push(r * W + 80);
  for (let c = 0; c < W; c++) v5.push(20 * W + c);
  return { res: 0.05, floors: [{ W, H, ymax: 4.0 }],
           voxels: [{ 4: v4, 5: v5, 6: [60 * W + 10], 7: [70 * W + 50] }] };
}
const R = makeRaster(house(), 0);
const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

test('raster marks codes 4, 5, 6 as walls but not 7', () => {
  assert.equal(R.wall[0 * 100 + 80], 1);
  assert.equal(R.wall[20 * 100 + 3], 1);
  assert.equal(R.wall[60 * 100 + 10], 1);
  assert.equal(R.wall[70 * 100 + 50], 0);
});

test('wall distances to faces in all four directions', () => {
  const d = wallDistances(R, 3.5, 2.0);       // row 40, col 70
  near(d.east.face, 4.00); near(d.east.d, 0.50);
  assert.equal(d.west, null);                 // nothing to the west inside the floor
  near(d.north.face, 2.95); near(d.north.d, 0.95);
  assert.equal(d.south, null);
});

test('point inside a wall has no distances', () => {
  const d = wallDistances(R, 4.02, 2.0);
  assert.deepEqual(d, { east: null, west: null, north: null, south: null });
});

test('nearest x and y pick the closer side', () => {
  const n = nearestXY(R, 4.5, 2.0);           // east of the interior wall
  assert.equal(n.x.side, 'west'); near(n.x.face, 4.05); near(n.x.d, 0.45);
  assert.equal(n.y.side, 'north');
});

test('snap to one wall, to a corner, and not beyond the radius', () => {
  const off = 12 * 0.0254;
  let s = snapPoint(R, 3.6, 1.0, off, 0.6);   // 0.40 m from the wall east, 1.95 m from the north wall
  near(s.x, 4.0 - off); near(s.y, 1.0);
  near(s.snapped.x, 4.0); assert.equal(s.snapped.y, null);
  s = snapPoint(R, 3.6, 2.6, off, 0.6);       // near both: corner
  near(s.x, 4.0 - off); near(s.y, 2.95 - off);
  s = snapPoint(R, 2.0, 1.0, off, 0.6);       // 2 m from the wall: untouched
  near(s.x, 2.0); near(s.y, 1.0); assert.equal(s.snapped.x, null);
});

test('nudge steps in both units', () => {
  const p = nudge({ x: 1, y: 1 }, 'E', STEP.in);
  near(p.x, 1.0254); near(p.y, 1);
  const q = nudge({ x: 1, y: 1 }, 'N', STEP.cm);
  near(q.y, 1.1); near(q.x, 1);
  const r = nudge(nudge({ x: 1, y: 1 }, 'S', STEP.cm), 'W', STEP.cm);
  near(r.x, 0.9); near(r.y, 0.9);
});

test('length formatting', () => {
  assert.equal(fmtLen(0.3048, 'in'), '12.0 in');
  assert.equal(fmtLen(1.0, 'cm'), '100 cm');
  assert.equal(fmtLen(0.456, 'cm'), '45.6 cm');
});

test('a floor whose frame starts left of x = 0 snaps to the same walls, shifted (house as data, 2026-09-30)', () => {
  const h = house();
  h.floors[0].x0 = -1.5;
  const S = makeRaster(h, 0);
  const d = wallDistances(S, 3.5 - 1.5, 2.0);  // the same cell as above, 1.5 m to the west
  near(d.east.face, 4.00 - 1.5); near(d.east.d, 0.50);
});

test('the Calibrate tab starts on a floor the house has (A5 clean room: a one-floor house)', async () => {
  const { startFloor } = await import('../rtls/static/calib_geom.js');
  assert.equal(startFloor(1, 1), 0);
  assert.equal(startFloor(1, 3), 1);
  assert.equal(startFloor(5, 3), 2);
  assert.equal(startFloor(undefined, 2), 0);
});

test('a calibration tag is shown by its device name, with its round id (A5 clean room: the rig offered "tag1")', async () => {
  const { tagName } = await import('../rtls/static/calib_geom.js');
  assert.equal(tagName({ id: 'tag1', label: 'Keys' }, 'k'), 'Keys (tag1)');
  assert.equal(tagName({ id: 'tag2', label: 'tag2' }, 'k'), 'tag2');
  assert.equal(tagName(undefined, '00:00:5e:00:53:a1'), '00:00:5e:00:53:a1');
});

test('every tab that opens on a remembered floor keeps to the floors the house has (A5 final review: Outlets)', async () => {
  const { readFileSync, readdirSync } = await import('node:fs');
  const dir = new URL('../rtls/static/', import.meta.url);
  for (const f of readdirSync(dir).filter((n) => n.endsWith('.js'))) {
    const src = readFileSync(new URL(f, dir), 'utf8');
    assert.ok(!/setFloor\(S\.floor\)/.test(src), `${f} opens on S.floor without startFloor`);
  }
});
