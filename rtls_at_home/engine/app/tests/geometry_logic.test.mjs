// node --test app/tests/
import test from 'node:test';
import assert from 'node:assert/strict';
import { wallColumnBands, planSymbols, northArrow, wallRects, exteriorApertures, rectBox } from '../rtls/static/geometry_logic.js';

test('a wall column with a window splits into wall / glass / wall at the sill and head', () => {
  assert.deepEqual(wallColumnBands({ code: 6, sill: 0.914, head: 2.032 }, 2.74),
    [{ from: 0, to: 0.914, code: 4 }, { from: 0.914, to: 2.032, code: 6 }, { from: 2.032, to: 2.74, code: 4 }]);
});

test('columns take their own sill and head', () => {
  const door = wallColumnBands({ code: 7, sill: 0, head: 2.032 }, 2.74);
  const high = wallColumnBands({ code: 6, sill: 1.5, head: 2.2 }, 2.74);
  assert.equal(door[0].code, 7); assert.equal(door[0].from, 0); assert.equal(door[1].code, 4);
  assert.equal(high[0].to, 1.5); assert.equal(high[1].code, 6);
});

test('a plain wall is one band; an opening to the ceiling is an empty column', () => {
  assert.deepEqual(wallColumnBands({ code: 0 }, 2.74), [{ from: 0, to: 2.74, code: 4 }]);
  assert.deepEqual(wallColumnBands({ code: 0, sill: 0, head: 2.74 }, 2.74), [{ from: 0, to: 2.74, code: 4 }]);
});

test('plan symbols: windows are lines, doors and openings are gaps - no swings (Nick 2026-09-26)', () => {
  const s = planSymbols([{ kind: 'window', a: [0, 0], b: [1, 0] }, { kind: 'door', a: [2, 0], b: [3, 0] },
                         { kind: 'opening', a: [4, 0], b: [5, 0] }]);
  assert.deepEqual(s.map(x => x.kind), ['window', 'door', 'opening']);
  assert.ok(s.every(x => !x.swing));
});

test('the north arrow points up the plan (the office north wall is the top of the map)', () => {
  assert.deepEqual(northArrow(), { dx: 0, dy: -1, label: 'N' });
});

// stairs (Nick 2026-09-26): a flight drawn on its lower floor at true pitch; in plan the architect's symbol
import { stairSymbol } from '../rtls/static/geometry_logic.js';

test('the plan symbol for a flight is one tread line per tread across the well and an arrow up the run', () => {
  const ramp = { lower: 0, x0: 1.0, y0: 2.0, x1: 5.0, y1: 3.0, axis: 'x', run: [1.5, 4.5], z0: 0, z1: 2.7, treads: 3 };
  const s = stairSymbol(ramp);
  assert.equal(s.treads.length, 3);                                  // 3 treads -> 3 lines, at the risers past the first
  assert.deepEqual(s.treads[0], { a: [2.5, 2.0], b: [2.5, 3.0] });   // first riser line 1.5 + 3.0/3 = 2.5 across y0..y1
  assert.deepEqual(s.treads[2], { a: [4.5, 2.0], b: [4.5, 3.0] });   // the last one is the top riser
  assert.deepEqual(s.arrow, { a: [1.5, 2.5], b: [4.5, 2.5] });       // along the run's centre line, bottom -> top
  assert.equal(s.label, 'UP');
});

test('the well outline on the upper floor is the well footprint with a DN arrow the other way', () => {
  const ramp = { lower: 0, x0: 1.0, y0: 2.0, x1: 5.0, y1: 3.0, axis: 'x', run: [1.5, 4.5], z0: 0, z1: 2.7, treads: 3 };
  const s = stairSymbol(ramp, { upper: true });
  assert.deepEqual(s.arrow, { a: [4.5, 2.5], b: [1.5, 2.5] });
  assert.equal(s.label, 'DN');
  assert.deepEqual(s.well, [[1.0, 2.0], [5.0, 2.0], [5.0, 3.0], [1.0, 3.0]]);
});

// rendering fixes (Nick 2026-09-26): the piers above and below a window keep the cell's own wall code, and wall cells
// merge into rectangles so a wall face is one box, not hundreds of coplanar 5 cm faces that z-fight
test('an exterior wall column with a window keeps the exterior code above and below the glass', () => {
  assert.deepEqual(wallColumnBands({ code: 6, sill: 0.914, head: 2.032 }, 2.74, 5),
    [{ from: 0, to: 0.914, code: 5 }, { from: 0.914, to: 2.032, code: 6 }, { from: 2.032, to: 2.74, code: 5 }]);
  assert.deepEqual(wallColumnBands({ code: 0 }, 2.74, 5), [{ from: 0, to: 2.74, code: 5 }]);
});

test('wall cells merge into the fewest rectangles: runs along a row, then equal runs stacked down rows', () => {
  const W = 10;
  // a 2-row x 3-column block, a lone cell, and an L (3 wide on row 5, 1 wide on rows 6-7 under its first column)
  const cells = [5, 6, 7, 15, 16, 17, 22, 50, 51, 52, 60, 70];
  assert.deepEqual(wallRects(cells, W), [
    { r: 0, c: 5, w: 3, h: 2 }, { r: 2, c: 2, w: 1, h: 1 }, { r: 5, c: 0, w: 3, h: 1 }, { r: 6, c: 0, w: 1, h: 2 }]);
  assert.deepEqual(wallRects([], W), []);
});

// an aperture's cells are never in the wall lists (they carry the glass/door code), so whether its piers are exterior
// comes from the wall it sits in: any cell of the connected aperture touching a plain exterior cell makes the whole
// aperture exterior
test('an aperture is exterior when the run it belongs to touches an exterior wall cell', () => {
  const W = 10;
  const exterior = new Set([3, 8]);                    // wall cells at both ends of a window on row 0
  const window = [4, 5, 6, 7];                         // the window's cells between them
  const interiorDoor = [24, 25];                       // a door on row 2 next to interior wall cells only
  const ext = exteriorApertures(window.concat(interiorDoor), exterior, W);
  assert.deepEqual([...ext].sort((a, b) => a - b), [4, 5, 6, 7]);
});

// where a merged rectangle's box goes in the scene (x east, z = -y): the rectangle's NW cell corner is (c*res, ymax -
// r*res) and the box extends east by w cells and SOUTH (toward +z) by h cells - the bug of 2026-09-26 extended it north
test('a rectangle box spans its cells eastward and southward from the north-west corner', () => {
  const res = 0.05, ymax = 5;
  const near = (a, b) => Object.keys(b).forEach(k => assert.ok(Math.abs(a[k] - b[k]) < 1e-9, `${k}: ${a[k]} vs ${b[k]}`));
  near(rectBox({ r: 0, c: 0, w: 1, h: 1 }, res, ymax), { x: 0, z: -5, sx: 0.05, sz: 0.05 });
  near(rectBox({ r: 2, c: 4, w: 2, h: 3 }, res, ymax), { x: 0.2, z: -4.9, sx: 0.1, sz: 0.15 });   // rows 2-4: y 4.75..4.90
});

test('a pane is 40% of the wall thickness, centred across the short side', () => {
  const p = rectBox({ r: 2, c: 4, w: 6, h: 2 }, 0.05, 5, { pane: true });   // an E-W wall 2 cells thick
  assert.ok(Math.abs(p.sx - 0.3) < 1e-9); assert.ok(Math.abs(p.sz - 0.04) < 1e-9); assert.ok(Math.abs(p.z - (-4.9 + 0.03)) < 1e-9);
  const q = rectBox({ r: 2, c: 4, w: 2, h: 6 }, 0.05, 5, { pane: true });   // a N-S wall: thin in x
  assert.ok(Math.abs(q.sx - 0.04) < 1e-9); assert.ok(Math.abs(q.x - (0.2 + 0.03)) < 1e-9); assert.ok(Math.abs(q.sz - 0.3) < 1e-9);
});

// House as data (2026-09-30): any number of floors and a frame origin that is not (0, 0).
import { floorColor, houseCentre, cellToXY, xyToCell } from '../rtls/static/geometry_logic.js';

test('floor colours cycle for any number of floors', () => {
  assert.equal(floorColor(0), floorColor(6));
  assert.notEqual(floorColor(0), floorColor(1));
});

test('cells and metres convert through the frame origin', () => {
  const fl = { x0: -2, y0: -1, ymax: 4, W: 120, H: 101 };
  assert.deepEqual(cellToXY(fl, 0, 0), { x: -2 + 0.025, y: 4 - 0.025 });
  assert.deepEqual(xyToCell(fl, -2 + 0.025, 4 - 0.025), { r: 0, c: 0 });
  assert.deepEqual(xyToCell({ ymax: 4 }, 0.06, 3.9), { r: 2, c: 1 });           // no origin: (0, 0)
});

test('the house centre spans every floor', () => {
  const fl = { x0: -2, y0: -1, ymax: 4, W: 120 };
  assert.deepEqual(houseCentre({ floors: [fl, { ...fl, x0: 0, ymax: 6 }] }), { x: 2, y: 2.5 });   // x -2..6, y -1..6
});

test('a wall box sits at the frame origin', () => {
  const a = rectBox({ r: 2, c: 4, w: 2, h: 6 }, 0.05, 5);
  const b = rectBox({ r: 2, c: 4, w: 2, h: 6 }, 0.05, 5, { x0: -1.5 });
  assert.ok(Math.abs(b.x - (a.x - 1.5)) < 1e-9 && b.z === a.z);
});

// Final review (2026-09-30): the plan view scales on the raster's depth, not on ymax, so a floor reaching below y = 0
// fits on the canvas.
import { planFrame } from '../rtls/static/geometry_logic.js';

test('the plan view fits a floor whose frame starts below y = 0', () => {
  const f = { x0: -2, y0: -1, ymax: 4, W: 81, H: 101 };
  const p = planFrame(f, 0.05, 800, 600, 40);
  assert.ok(p.Y(-1) <= 600 - 40 + 1e-9 && p.Y(4) >= 40 - 1e-9 && p.X(-2) >= 40 - 1e-9 && p.X(-2 + 81 * 0.05) <= 800 - 40 + 1e-9);
});
