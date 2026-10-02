// node --test app/tests/map_logic.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import * as L from '../rtls/static/map_logic.js';

const near = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);
const sq = (x0, y0, x1, y1) => [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
function doc() {
  return { schema: 1, frame: { x0: 0, y0: 0, res: 0.05 }, display_units: 'ft-in', stairs: [], receivers: [], anchors: [],
           floors: [{ id: 'f0', name: 'Ground', elevation: 0, ceiling: 2.4, slab: 0.3, open_edges: [], apertures: [], objects: [],
                      rooms: [{ id: 'a', name: 'A', kind: 'room', outline: sq(0, 0, 3, 3) },
                              { id: 'b', name: 'B', kind: 'room', outline: sq(3, 0, 5, 3) }] }] };
}

test('lengths parse in feet-and-inches and metres, and junk is refused', () => {
  near(L.parseLength(`12'6"`), 3.81); near(L.parseLength(`12' 6"`), 3.81); near(L.parseLength(`12'`), 3.6576);
  near(L.parseLength(`150"`), 3.81); near(L.parseLength(`6"`), 0.1524); near(L.parseLength(`0'6"`), 0.1524);
  near(L.parseLength('3.8 m'), 3.8); near(L.parseLength('3.8m'), 3.8); near(L.parseLength('380 cm'), 3.8); near(L.parseLength('3.8'), 3.8);
  for (const bad of ['', 'abc', '-2', '0', `0'`, '3..8']) assert.equal(L.parseLength(bad), null, bad);
  assert.deepEqual(L.parseSize('4.2 x 3.6'), [4.2, 3.6]);
  const s = L.parseSize(`12' x 10'6"`); near(s[0], 3.6576); near(s[1], 3.2004);
  assert.equal(L.parseSize('4.2 by'), null);
});

test('lengths format to the nearest half inch or centimetre', () => {
  assert.equal(L.fmtLength(3.81, 'ft-in'), `12' 6"`);
  assert.equal(L.fmtLength(0.1651, 'ft-in'), `0' 6½"`);
  assert.equal(L.fmtLength(3.8, 'm'), '3.80 m');
});

test('rectangles, areas, containment and the nearest wall', () => {
  assert.deepEqual(L.rectOutline([3, 2], [1, 0]), sq(1, 0, 3, 2));
  near(L.polyArea(sq(0, 0, 3, 2)), 6);
  assert.ok(L.pointInPoly([1, 1], sq(0, 0, 3, 3)) && !L.pointInPoly([4, 1], sq(0, 0, 3, 3)));
  const h = L.nearestEdge(doc(), 0, [2.95, 1.2], 0.2);
  assert.equal(h.room, 'a'); near(h.point[0], 3); near(h.point[1], 1.2); near(h.dist, 0.05);
  assert.equal(L.nearestEdge(doc(), 0, [1.5, 1.5], 0.2), null);
});

test('two rooms share the segment where their walls lie on one line', () => {
  const d = doc(), s = L.sharedSegment(d.floors[0].rooms[0], d.floors[0].rooms[1]);
  assert.ok(s); near(s.a[0], 3); near(s.b[0], 3); near(Math.abs(s.a[1] - s.b[1]), 3);
});

test('setting an edge length keeps a rectangle a rectangle', () => {
  const r = L.setEdgeLength(sq(0, 0, 3, 2), 0, 4);              // the bottom edge 3 -> 4 m
  assert.deepEqual(L.bbox(r), { x0: 0, y0: 0, x1: 4, y1: 2 });
  assert.equal(r.length, 4);
});

test('snapping prefers a corner, then a wall, then the grid; ortho locks an axis', () => {
  const ctx = { grid: 0.1, vertices: [[3, 3]], edges: [[[0, 0], [5, 0]]], from: null, ortho: false, tol: 0.15 };
  assert.deepEqual(L.snap([3.1, 2.9], ctx).p, [3, 3]);
  assert.deepEqual(L.snap([1.23, 0.08], ctx), { p: [1.23, 0], kind: 'edge' });
  assert.deepEqual(L.snap([1.23, 1.77], ctx), { p: [1.2, 1.8], kind: 'grid' });
  const o = L.snap([2.04, 0.9], { ...ctx, vertices: [], edges: [], from: [1, 1], ortho: true });
  assert.deepEqual(o.p, [2.0, 1.0]);
});

test('edits return new documents: rooms, objects, apertures, open edges, walls', () => {
  const d0 = doc();
  const d1 = L.addRoom(d0, 0, sq(0, 3, 2, 5));
  assert.equal(d0.floors[0].rooms.length, 2);                    // not mutated
  const r = d1.floors[0].rooms[2]; assert.equal(r.name, 'Room 3'); assert.ok(r.id && r.id !== 'a' && r.id !== 'b');
  const d2 = L.addAperture(d1, 0, 'door', L.nearestEdge(d1, 0, [3.0, 0.2], 0.2));
  const ap = d2.floors[0].apertures[0];
  assert.equal(ap.kind, 'door'); near(Math.hypot(ap.b[0] - ap.a[0], ap.b[1] - ap.a[1]), 0.81);
  assert.ok(Math.min(ap.a[1], ap.b[1]) >= 0 - 1e-9);            // clamped inside the 0..3 m edge
  const d3 = L.addOpenEdge(d2, 0, 'a', 'b');
  assert.deepEqual([...d3.floors[0].open_edges[0].rooms].sort(), ['a', 'b']);
  const d4 = L.addWall(d3, 0, [1, 0.5], [1, 2.5]);
  const w = d4.floors[0].objects.at(-1);
  assert.equal(w.material, 'drywall'); assert.equal(w.construction, 'panel'); assert.equal(w.room, 'a'); near(w.height, 2.4);
  const d5 = L.addObject(d4, 0, sq(0.2, 0.2, 0.9, 0.9), 'Fridge');
  const f = d5.floors[0].objects.at(-1);
  assert.equal(f.name, 'Fridge'); assert.equal(f.material, 'metal'); assert.equal(f.room, 'a');
});

test('floors add above and below with elevations that make room', () => {
  const d1 = L.addFloor(doc(), 'above', 0);
  assert.equal(d1.floors.length, 2); near(d1.floors[1].elevation, 2.7);
  const d2 = L.addFloor(d1, 'below', 0);
  assert.equal(d2.floors[0].elevation, 0); near(d2.floors[1].elevation, 2.7); near(d2.floors[2].elevation, 5.4);
  assert.equal(new Set(d2.floors.map(f => f.id)).size, 3);
});

test('stairs make a room on each floor and the record linking them', () => {
  const d1 = L.addFloor(doc(), 'above', 0);
  const d2 = L.addStairs(d1, 0, sq(0, 3, 1, 6), '+y');
  const st = d2.stairs[0];
  assert.equal(st.lower.floor, 'f0'); assert.equal(st.upper.floor, d2.floors[1].id);
  assert.equal(st.run.axis, 'y'); near(st.run.from, 3); near(st.run.to, 6);
  assert.equal(d2.floors[1].rooms.find(r => r.id === st.upper.room).kind, 'stairs');
});

test('deleting a room takes what depends on it', () => {
  let d = L.addOpenEdge(doc(), 0, 'a', 'b');
  d = L.addObject(d, 0, sq(0.2, 0.2, 0.9, 0.9), 'Fridge');
  d = L.deleteItem(d, { kind: 'room', fi: 0, id: 'a' });
  assert.deepEqual(d.floors[0].rooms.map(r => r.id), ['b']);
  assert.equal(d.floors[0].open_edges.length, 0); assert.equal(d.floors[0].objects.length, 0);
});

test('undo and redo walk the history', () => {
  const h = L.history(3), d0 = doc(), d1 = L.addRoom(d0, 0, sq(0, 3, 2, 5));
  h.push(d0);
  assert.deepEqual(h.undo(d1), d0); assert.deepEqual(h.redo(d0), d1);
  assert.equal(h.redo(d1), null);
});

test('steps, sills and heights off the floor take zero, and steps can be negative', () => {
  assert.equal(L.parseOffset('0'), 0); assert.equal(L.parseOffset(`0'`), 0); assert.equal(L.parseOffset(''), null);
  near(L.parseOffset(`-7"`, { signed: true }), -0.1778); assert.equal(L.parseOffset(`-7"`), null);
  near(L.parseOffset('0.9 m'), 0.9); assert.equal(L.parseOffset('abc'), null);
});

test('a click hits the topmost thing: object, then aperture, then open wall, then room', () => {
  let d = L.addObject(doc(), 0, sq(0.2, 0.2, 0.9, 0.9), 'Fridge');
  d = L.addAperture(d, 0, 'door', L.nearestEdge(d, 0, [3.0, 1.5], 0.2));
  d = L.addOpenEdge(d, 0, 'a', 'b');
  assert.deepEqual(L.hitTest(d, 0, [0.5, 0.5], 0.1), { kind: 'object', fi: 0, id: 'fridge' });
  assert.equal(L.hitTest(d, 0, [3.02, 1.5], 0.1).kind, 'aperture');
  assert.deepEqual(L.hitTest(d, 0, [3.02, 2.8], 0.1), { kind: 'open', fi: 0, id: d.floors[0].open_edges[0].id });
  assert.deepEqual(L.hitTest(d, 0, [1.5, 2.5], 0.1), { kind: 'room', fi: 0, id: 'a' });
  assert.equal(L.hitTest(d, 0, [9, 9], 0.1), null);
});

test('walls shared with another indoor room are interior, even when only part of an edge is shared', () => {
  const f = doc().floors[0];
  let w = L.wallEdges(f);
  assert.equal(w.filter((e) => !e.exterior).length, 2);          // x = 3, once from each room
  assert.ok(w.filter((e) => !e.exterior).every((e) => Math.abs(e.a[0] - 3) < 1e-9 && Math.abs(e.b[0] - 3) < 1e-9));
  f.rooms.push({ id: 'c', name: 'C', kind: 'room', outline: sq(0, 3, 1.5, 5) });
  w = L.wallEdges(f).filter((e) => e.room === 'a' && Math.abs(e.a[1] - 3) < 1e-9 && Math.abs(e.b[1] - 3) < 1e-9);
  const span = (e) => [Math.min(e.a[0], e.b[0]), Math.max(e.a[0], e.b[0])];
  assert.deepEqual(w.filter((e) => !e.exterior).map(span), [[0, 1.5]]);
  assert.deepEqual(w.filter((e) => e.exterior).map(span), [[1.5, 3]]);
});

test('a rectangle takes a typed width and depth from its lower-left corner', () => {
  const r = L.setRectSize(sq(1, 1, 3, 2), 4, 0.5);
  assert.deepEqual(L.bbox(r), { x0: 1, y0: 1, x1: 5, y1: 1.5 });
  assert.ok(L.isRect(r) && !L.isRect([[0, 0], [1, 0], [1, 1]]));
});

test("an aperture's width and offset along its wall can be typed", () => {
  const d = L.addAperture(doc(), 0, 'window', L.nearestEdge(doc(), 0, [1.5, 0.02], 0.2));   // a's bottom wall
  const ref = { kind: 'aperture', fi: 0, id: 'window' }, s = L.apertureSpan(d, 0, L.itemOf(d, ref));
  near(s.width, 0.91); near(s.offset, 1.5 - 0.455); near(s.wall, 5);          // the south wall of a and b, 0..5
  const ap = L.itemOf(L.setApertureSpan(d, ref, { offset: 0.5, width: 1.2 }), ref);
  near(Math.min(ap.a[0], ap.b[0]), 0.5); near(Math.max(ap.a[0], ap.b[0]), 1.7); near(ap.a[1], 0);
  const ap2 = L.itemOf(L.setApertureSpan(d, ref, { offset: 4.5, width: 1.2 }), ref);   // kept on the wall
  near(Math.max(ap2.a[0], ap2.b[0]), 5);
});

test('turning the stairs runs them the other way across their room', () => {
  const d1 = L.addFloor(doc(), 'above', 0), d2 = L.addStairs(d1, 0, sq(0, 3, 1, 6), '+y');
  const st = L.setStairsDir(d2, d2.stairs[0].id, '-y').stairs[0];
  near(st.run.from, 6); near(st.run.to, 3); assert.ok(st.foot[1] > st.top[1]);
  assert.equal(L.stairsDir(st), '-y');
});

test('moving a room takes its objects, the doors and windows on its own walls, and stairs that start in it', () => {
  let d = L.addObject(doc(), 0, sq(0.2, 0.2, 0.9, 0.9), 'Fridge');
  d = L.addAperture(d, 0, 'window', L.nearestEdge(d, 0, [1.5, 0.02], 0.2));        // a's outside wall
  d = L.addAperture(d, 0, 'door', L.nearestEdge(d, 0, [3.0, 1.5], 0.2));           // the wall a shares with b
  d = L.addFloor(d, 'above', 0);
  d = L.addStairs(d, 0, sq(0, -2, 1, 0), '+y');
  d = L.moveRoom(L.moveRoom(d, 0, 'a', -1, 0), 0, d.stairs[0].lower.room, 0, -1);
  const f = d.floors[0];
  near(f.objects[0].outline[0][0], -0.8); near(f.apertures.find((a) => a.kind === 'window').a[0], 1.045 - 1);
  near(f.apertures.find((a) => a.kind === 'door').a[0], 3);
  const st = d.stairs[0]; near(st.run.from, -3); near(st.foot[1], -3 + 0.15);
});

test('with align, a point lines up with a nearby corner in x or y when no corner or wall is within reach', () => {
  const ctx = { grid: 0.1, vertices: [[3, 3], [0, 7]], edges: [], tol: 0.15, align: true };
  assert.deepEqual(L.snap([3.08, 5.23], ctx), { p: [3, 5.2], kind: 'align' });
  assert.deepEqual(L.snap([3.08, 6.9], ctx), { p: [3, 7], kind: 'align' });
  assert.deepEqual(L.snap([3.08, 5.23], { ...ctx, align: false }), { p: [3.1, 5.2], kind: 'grid' });
});

// ---------- the final review's fixes ----------
test('text for the page is escaped for attributes too: a feet-and-inches length keeps its inch mark', () => {
  assert.equal(L.escHtml(`12' 6"`), '12&#39; 6&quot;');
  assert.equal(L.escHtml(`x" onfocus="alert(1)`), 'x&quot; onfocus=&quot;alert(1)');
  assert.equal(L.escHtml('<b>&'), '&lt;b&gt;&amp;'); assert.equal(L.escHtml(null), '');
});

test('the frame moves out to take in a room drawn past the origin, and only then', () => {
  const d = doc(); d.frame = { x0: 0, y0: 0, res: 0.05 };
  assert.equal(L.withFrame(d), d);                                           // nothing outside: the same document
  d.floors[0].rooms[0].outline = sq(-0.051, -0.013, 3, 3);                  // the converted house's traced walls
  assert.equal(L.withFrame(d), d);
  const f = L.withFrame(L.addRoom(d, 0, sq(-2.31, 0, 0, 3))).frame;
  near(f.x0, -2.35); near(f.y0, 0); near(f.res, 0.05);
});

test('an uploaded image goes on the floor it was uploaded for, keeping a layout it already has', () => {
  const d0 = L.addFloor(doc(), 'above', 0);
  const d1 = L.setUnderlay(d0, 'f0', 'underlays/f0-1.png', [1000, 500], { cx: 0, cy: 0, w: 10 });
  const u = d1.floors[0].underlay;
  assert.equal(u.file, 'underlays/f0-1.png'); near(u.m_per_px, 0.005); near(u.x, 0); near(u.y, 3); near(u.opacity, 0.5);
  assert.equal(d1.floors[1].underlay, undefined);
  const e = L.setUnderlay(d1, d1.floors[1].id, 'underlays/up.png', [100, 50], { cx: 1, cy: 2, w: 10 }).floors[1].underlay;
  near(e.m_per_px, 0.1); near(e.x, -4); near(e.y, 4.5);                       // an empty floor: across the view
  const moved = L.updateItem(d1, { kind: 'floor', fi: 0 }, { underlay: { ...u, m_per_px: 0.0128, rotation: 90 } });
  const k = L.setUnderlay(moved, 'f0', 'underlays/f0-2.png', [10, 10], null).floors[0].underlay;
  assert.equal(k.file, 'underlays/f0-2.png'); near(k.m_per_px, 0.0128); assert.equal(k.rotation, 90);
  assert.equal(L.setUnderlay(d0, 'gone', 'x.png', [1, 1], null), null);     // the floor was deleted meanwhile
});

function fakeClock() {
  let t = 0, q = [];
  return {
    schedule: (fn, ms) => { const h = { fn, at: t + ms }; q.push(h); return h; },
    cancel: (h) => { q = q.filter((x) => x !== h); },
    async tick(ms) { t += ms; const due = q.filter((h) => h.at <= t); q = q.filter((h) => h.at > t); due.forEach((h) => h.fn()); await settle(); },
  };
}
const settle = () => new Promise((r) => setImmediate(r));
function deferred() { let res, rej; const p = new Promise((a, b) => { res = a; rej = b; }); return { p, res, rej }; }

test('the autosave sends once, after the last edit, and hands on the reply', async () => {
  const c = fakeClock(), replies = [];
  let n = 0;
  const s = L.createSaver({ send: () => { n++; return Promise.resolve({ problems: [] }); }, onSaved: (r) => replies.push(r), schedule: c.schedule, cancel: c.cancel });
  s.changed(); await c.tick(300); s.changed(); await c.tick(300);
  assert.equal(n, 0); assert.equal(s.state.dirty, true);
  await c.tick(400);
  assert.equal(n, 1); assert.equal(replies.length, 1); assert.equal(s.state.dirty, false);
});

test('an edit made while a save is out goes in a second save after it', async () => {
  const c = fakeClock(), d = [deferred(), deferred()];
  let n = 0;
  const s = L.createSaver({ send: () => d[n++].p, schedule: c.schedule, cancel: c.cancel });
  s.changed(); await c.tick(700); assert.equal(n, 1);
  s.changed(); await c.tick(700); assert.equal(n, 1);                        // the first is still out
  d[0].res({}); await settle(); assert.equal(n, 2); assert.equal(s.state.dirty, true);
  d[1].res({}); await settle(); assert.equal(s.state.dirty, false);
});

test('a discard waits for a save still out, then drops its reply', async () => {
  const c = fakeClock(), d = deferred(), replies = [];
  const s = L.createSaver({ send: () => d.p, onSaved: (r) => replies.push(r), schedule: c.schedule, cancel: c.cancel });
  s.changed(); await c.tick(700);
  let done = false;
  const r = s.reset().then(() => { done = true; });
  await settle(); assert.equal(done, false);                                 // the PUT lands before the DELETE goes
  d.res({ problems: ['stale'] }); await r;
  assert.deepEqual(replies, []); assert.equal(s.state.dirty, false); assert.equal(s.state.saving, false);
});

test('a failed save is retried with backoff until it lands', async () => {
  const c = fakeClock();
  let n = 0;
  const s = L.createSaver({ send: () => (++n < 3 ? Promise.reject(new Error('Failed to fetch')) : Promise.resolve({})),
                            schedule: c.schedule, cancel: c.cancel, backoff: [1000, 3000] });
  s.changed(); await c.tick(700);
  assert.equal(n, 1); assert.equal(s.state.error, 'Failed to fetch'); assert.equal(s.state.dirty, true);
  await c.tick(1000); assert.equal(n, 2);
  await c.tick(3000); assert.equal(n, 3); assert.equal(s.state.error, null); assert.equal(s.state.dirty, false);
});

// ---------- closing out the map editor's open issues ----------
test('an aperture drags along its wall: an end resizes it, the middle slides it', () => {
  const d = L.addAperture(doc(), 0, 'window', L.nearestEdge(doc(), 0, [1.5, 0.02], 0.2));   // 1.045 .. 1.955
  const ref = { kind: 'aperture', fi: 0, id: 'window' };
  const span = (x) => { const a = L.itemOf(x, ref); return [Math.min(a.a[0], a.b[0]), Math.max(a.a[0], a.b[0])]; };
  let s = span(L.dragAperture(d, ref, 'end', [2.4, 0.3], [1.955, 0]));      // the right end out to 2.4
  near(s[0], 1.045); near(s[1], 2.4);
  s = span(L.dragAperture(d, ref, 'end', [0.5, -0.1], [1.045, 0]));        // the left end out to 0.5
  near(s[0], 0.5); near(s[1], 1.955);
  s = span(L.dragAperture(d, ref, 'end', [1.9, 0], [1.045, 0]));           // never thinner than 10 cm
  near(s[1] - s[0], 0.1); near(s[1], 1.955);
  s = span(L.dragAperture(d, ref, 'body', [2.5, 0], [1.5, 0]));            // slid 1 m along
  near(s[0], 2.045); near(s[1], 2.955);
  s = span(L.dragAperture(d, ref, 'body', [9, 0], [1.5, 0]));              // kept on the wall
  near(s[1], 5);
});

// Nick 2026-10-01: a window behind a cabinet would not drag past the kitchen to the bar - rooms are logical, the
// exterior wall is one.
test('a window on an exterior wall slides past the room behind it, to where the wall ends', () => {
  const d = L.addAperture(doc(), 0, 'window', L.nearestEdge(doc(), 0, [1.5, 0.02], 0.2));   // a's stretch, 1.045 .. 1.955
  const ref = { kind: 'aperture', fi: 0, id: 'window' };
  const span = (x) => { const a = L.itemOf(x, ref); return [Math.min(a.a[0], a.b[0]), Math.max(a.a[0], a.b[0])]; };
  let s = span(L.dragAperture(d, ref, 'body', [4.5, 0], [1.5, 0]));        // into b's stretch
  near(s[0], 4.045); near(s[1], 4.955);
  s = span(L.dragAperture(d, ref, 'end', [4.8, 0], [1.955, 0]));           // an end too
  near(s[0], 1.045); near(s[1], 4.8);
});

test('an exterior wall stops where it becomes an inside wall; an inside wall runs along its own stretch', () => {
  const dc = doc();
  dc.floors[0].rooms.push({ id: 'c', name: 'C', kind: 'room', outline: sq(3, 3, 5, 5) });   // north of b
  const d = L.addAperture(dc, 0, 'window', L.nearestEdge(dc, 0, [1.5, 2.98], 0.2));        // a's north wall, outside
  const ref = { kind: 'aperture', fi: 0, id: 'window' };
  near(L.apertureSpan(d, 0, L.itemOf(d, ref)).wall, 3);                    // b's north wall is c's: inside
  const ap = L.itemOf(L.dragAperture(d, ref, 'body', [9, 3], [1.5, 3]), ref);
  near(Math.max(ap.a[0], ap.b[0]), 3);
  const dd = L.addAperture(dc, 0, 'door', L.nearestEdge(dc, 0, [3.02, 1.5], 0.2));         // the a | b wall
  near(L.apertureSpan(dd, 0, L.itemOf(dd, { kind: 'aperture', fi: 0, id: 'door' })).wall, 3);   // c's west wall is outside
});

test('open walls carry ids, given to old ones on loading, so a selection survives edits to the others', () => {
  let d = L.addOpenEdge(doc(), 0, 'a', 'b');
  const e = d.floors[0].open_edges[0];
  assert.ok(e.id);
  assert.deepEqual(L.itemOf(d, { kind: 'open', fi: 0, id: e.id }), e);
  const legacy = doc();
  legacy.floors[0].open_edges = [{ a: [3, 0], b: [3, 3], rooms: ['a', 'b'] }, { a: [3, 0], b: [3, 1], rooms: ['a', 'b'] }];
  const withIds = L.ensureIds(legacy), ids = withIds.floors[0].open_edges.map((x) => x.id);
  assert.ok(ids[0] && ids[1] && ids[0] !== ids[1]); assert.equal(L.ensureIds(withIds), withIds);
  assert.equal(L.deleteItem(withIds, { kind: 'open', fi: 0, id: ids[0] }).floors[0].open_edges[0].id, ids[1]);
});

test('a floor moves up or down the stack and the elevations follow', () => {
  let d = L.addFloor(L.addFloor(doc(), 'above', 0), 'above', 1);           // 0, 2.7, 5.4
  d.floors[2].ceiling = 3.0;
  const ids = d.floors.map((f) => f.id);
  d = L.moveFloor(d, 2, -1);                                                // the top floor down one
  assert.deepEqual(d.floors.map((f) => f.id), [ids[0], ids[2], ids[1]]);
  near(d.floors[0].elevation, 0); near(d.floors[1].elevation, 2.7); near(d.floors[2].elevation, 6.0);
  assert.equal(L.moveFloor(d, 0, -1), d); assert.equal(L.moveFloor(d, 2, 1), d);
});

test('new ids and names count on past the ones taken', () => {
  const d = doc();
  d.floors[0].rooms[1].id = 'room3'; d.floors[0].rooms[1].name = 'Room 3';
  const r = L.addRoom(d, 0, sq(0, 3, 2, 5)).floors[0].rooms.at(-1);
  assert.equal(r.id, 'room4'); assert.equal(r.name, 'Room 4');
  const d2 = L.addFloor(doc(), 'above', 0);
  d2.floors[1].id = 'floor3'; d2.floors[1].name = 'Floor 3';
  const f = L.addFloor(d2, 'above', 1).floors[2];
  assert.equal(f.id, 'floor4'); assert.equal(f.name, 'Floor 4');
});

// ---------- receivers (spec 2026-10-01) ----------
const live = () => ({ id: 'p', name: 'P', floor: 'f0', room: 'a', x: 1, y: 1, height: 0.3, epochs: ['2026-09-01T10:00-07:00'], moves: [] });

test('a receiver moved in a draft gets one pending move; moving it back removes it', () => {
  let d = { ...doc(), receivers: [live()] };
  d = L.moveReceiver(d, 'p', 0, [2, 2], live(), '2026-10-01T09:00-07:00');
  let r = d.receivers[0];
  assert.deepEqual(r.moves, [{ at: '2026-10-01T09:00-07:00', floor: 'f0', room: 'a', x: 1, y: 1, height: 0.3 }]);
  assert.equal(r.x, 2); assert.equal(r.room, 'a');
  assert.deepEqual(L.pendingMove(r, live()).at, '2026-10-01T09:00-07:00');
  d = L.moveReceiver(d, 'p', 0, [2.5, 1.5], live(), '2026-10-01T09:05-07:00');
  assert.equal(d.receivers[0].moves.length, 1); assert.equal(d.receivers[0].moves[0].at, '2026-10-01T09:00-07:00');
  d = L.setMoveTime(d, 'p', '2026-09-30T18:00-07:00', live());
  assert.equal(d.receivers[0].moves[0].at, '2026-09-30T18:00-07:00');
  d = L.moveReceiver(d, 'p', 0, [1, 1], live(), '2026-10-01T09:10-07:00');
  assert.deepEqual(d.receivers[0].moves, []); assert.equal(L.pendingMove(d.receivers[0], live()), null);
  d = L.moveReceiver(d, 'p', 0, [3.5, 1], live(), '2026-10-01T09:20-07:00');   // into room b
  assert.equal(d.receivers[0].room, 'b');
  d = L.moveReceiver(d, 'p', 0, [3.5, 1], live(), '2026-10-01T09:30-07:00', 1.22);   // a new height is a move too
  assert.equal(d.receivers[0].height, 1.22); assert.equal(d.receivers[0].moves.length, 1);
});

test('epochs: pending ones can be removed, live ones cannot', () => {
  let d = { ...doc(), receivers: [live()] };
  d = L.addEpoch(d, 'p', '2026-10-01T09:00-07:00');
  assert.deepEqual(d.receivers[0].epochs, ['2026-09-01T10:00-07:00', '2026-10-01T09:00-07:00']);
  assert.deepEqual(L.pendingEpochs(d.receivers[0], live()), ['2026-10-01T09:00-07:00']);
  assert.equal(L.removeEpoch(d, 'p', '2026-09-01T10:00-07:00', live()).receivers[0].epochs.length, 2);
  assert.equal(L.removeEpoch(d, 'p', '2026-10-01T09:00-07:00', live()).receivers[0].epochs.length, 1);
});

test('a new receiver is placed with its room and can be removed; a live one cannot', () => {
  let d = L.addReceiver({ ...doc(), receivers: [] }, 0, [4, 1], { name: 'Kitchen Proxy', address: 'aa:bb', added: '2026-10-01T09:00-07:00' });
  const r = d.receivers[0];
  assert.equal(r.room, 'b'); assert.equal(r.floor, 'f0'); assert.equal(r.id, 'kitchen_proxy'); assert.equal(r.height, 0.3);
  assert.equal(r.address, 'aa:bb'); assert.equal(r.added, '2026-10-01T09:00-07:00');
  d = L.moveReceiver(d, r.id, 0, [4.2, 1], null, 'now');                          // not live yet: no move recorded
  assert.deepEqual(d.receivers[0].moves, []);
  assert.equal(L.removeReceiver(d, r.id, null).receivers.length, 0);
  assert.equal(L.removeReceiver(d, r.id, r).receivers.length, 1);
  assert.equal(L.addReceiver(d, 0, [1, 1], { name: 'Kitchen Proxy' }).receivers[1].id, 'kitchen_proxy2');
});

test('a receiver on the wall line belongs to the room it is against', () => {
  const d = L.addReceiver({ ...doc(), receivers: [] }, 0, [-0.1, 1], { name: 'Wall Proxy' });
  assert.equal(d.receivers[0].room, 'a');
  assert.equal(L.receiverAt(d, 0, [-0.05, 1.05], 0.2), 'wall_proxy'); assert.equal(L.receiverAt(d, 0, [2, 2], 0.2), null);
});

test('a receiver snaps to a measured outlet nearby and takes its height', () => {
  const outlets = [{ floor: 0, x: 2.0, y: 0.05, z_rel: 0.41 }, { floor: 1, x: 2.0, y: 0.05, z_rel: 1.2 }];
  const s = L.snapToOutlet([2.1, 0.2], 0, outlets, 0.3);
  assert.deepEqual(s.p, [2.0, 0.05]); assert.equal(s.height, 0.41);
  assert.equal(L.snapToOutlet([3, 2], 0, outlets, 0.3), null);
  near(L.snapToOutlet([2.1, 0.2], 0, outlets, 0.3, 0.0889).height, 0.4989);   // at the antenna, as the advisor places
});

test('put back: a moved receiver returns to where the live house has it; one placed in this draft has nowhere to go', () => {
  const live = { id: 'p', name: 'P', floor: 'f0', room: 'a', x: 1, y: 1, height: 0.3, epochs: ['2026-09-01T10:00-07:00'], moves: [] };
  let d = { ...doc(), receivers: [JSON.parse(JSON.stringify(live))] };
  d = L.moveReceiver(d, 'p', 0, [4, 1], live, '2026-10-01T09:00-07:00', 0.5);
  d = L.addEpoch(d, 'p', '2026-10-01T09:30-07:00');                            // a re-seat marked in the draft stays
  const b = L.putBack(d, 'p', live).receivers[0];
  assert.deepEqual([b.floor, b.room, b.x, b.y, b.height, b.moves], ['f0', 'a', 1, 1, 0.3, []]);
  assert.deepEqual(b.epochs, ['2026-09-01T10:00-07:00', '2026-10-01T09:30-07:00']);
  assert.equal(L.pendingMove(b, live), null);
  assert.equal(L.putBack(d, 'p', null), d); assert.equal(L.putBack(d, 'p', undefined), d); assert.equal(L.putBack(d, 'nope', live), d);
});

test('local times are written as the registry writes them', () => {
  const t = L.isoLocal(new Date(2026, 9, 1, 9, 5));
  assert.match(t, /^2026-10-01T09:05[+-]\d\d:\d\d$/);
});

test('a selected receiver is found by its ref', () => {
  const d = { ...doc(), receivers: [live()] };
  assert.equal(L.itemOf(d, { kind: 'receiver', fi: 0, id: 'p' }).name, 'P');
  assert.equal(L.itemOf(d, { kind: 'receiver', fi: 0, id: 'q' }), null);
});

// ---------- the receiver review's fixes ----------
test('without knowing the live house, a receiver is neither moved nor removed', () => {
  const d = { ...doc(), receivers: [live()] };
  assert.equal(L.moveReceiver(d, 'p', 0, [2, 2], undefined, 'now'), d);
  assert.equal(L.removeReceiver(d, 'p', undefined), d);
});

test('after restoring an older house, a receiver still gets one pending move', () => {
  const lv = { ...live(), x: 2, moves: [{ at: '2026-09-20T10:00-07:00', floor: 'f0', room: 'a', x: 1, y: 1, height: 0.3 }] };
  let d = { ...doc(), receivers: [live()] };                                     // the restored version: before that move
  d = L.moveReceiver(d, 'p', 0, [2.5, 2], lv, '2026-10-01T09:00-07:00');
  d = L.moveReceiver(d, 'p', 0, [2.6, 2], lv, '2026-10-01T09:05-07:00');
  assert.equal(d.receivers[0].moves.length, 1);
  assert.equal(L.pendingMove(d.receivers[0], lv).at, '2026-10-01T09:00-07:00');
  d = L.setMoveTime(d, 'p', '2026-09-30T08:00-07:00', lv);
  assert.equal(d.receivers[0].moves[0].at, '2026-09-30T08:00-07:00');
});

test('deleting a room leaves its receivers roomless; deleting a floor takes its receivers', () => {
  const r2 = { ...live(), id: 'q', name: 'Q', moves: [{ at: '2026-09-01T10:00-07:00', floor: 'f0', room: 'a', x: 1, y: 1, height: 0.3 }] };
  let d = { ...doc(), receivers: [live(), r2] };
  const dr = L.deleteItem(d, { kind: 'room', fi: 0, id: 'a' });
  assert.equal(dr.receivers[0].room, null); assert.equal(dr.receivers[1].moves[0].room, null);
  d = L.addFloor(d, 'above', 0);
  d.receivers.push({ ...live(), id: 'up', name: 'Up', floor: d.floors[1].id, room: null });
  assert.deepEqual(L.deleteItem(d, { kind: 'floor', fi: 1 }).receivers.map((r) => r.id), ['p', 'q']);
  assert.deepEqual(L.receiversOnFloor(d, d.floors[1].id).map((r) => r.id), ['up']);
});

test("a pending epoch's time can be set; a live one cannot", () => {
  let d = L.addEpoch({ ...doc(), receivers: [live()] }, 'p', '2026-10-01T09:00-07:00');
  d = L.setEpochTime(d, 'p', '2026-10-01T09:00-07:00', '2026-09-30T20:00-07:00', live());
  assert.deepEqual(d.receivers[0].epochs, ['2026-09-01T10:00-07:00', '2026-09-30T20:00-07:00']);
  assert.equal(L.setEpochTime(d, 'p', '2026-09-01T10:00-07:00', '2026-09-02T10:00-07:00', live()), d);
});

// ---------- the placement advisor's layout as a draft (spec 2026-10-01-placement-advisor) ----------
test('a suggested layout moves the proxies, places the new ones and keeps the draft', () => {
  const rx = (id, name, x) => ({ id, name, floor: 'f0', room: 'a', x, y: 1, height: 0.3, epochs: [], moves: [] });
  const applied = { ...doc(), receivers: [rx('p', 'P', 1), rx('q', 'Q', 2), rx('r', 'R', 2.5)] };
  let draft = JSON.parse(JSON.stringify(applied));
  draft.floors[0].rooms[1].name = 'B renamed';                                   // an edit already in the draft
  const result = { moves: [
    { candidate: 'out:o1', receiver: 'P', floor: 0, x: 4, y: 0.2, room: 'B', height: 0.4953 },
    { candidate: 'now:Q', receiver: 'Q', floor: 0, x: 2, y: 1, room: 'A', height: 0.3 },
    { candidate: 'out:o2', receiver: null, floor: 0, x: 0.2, y: 2.5, room: 'A', height: 1.2065 },
    { candidate: 'out:o3', receiver: 'Gone', floor: 0, x: 1, y: 1, room: 'A', height: 0.4953 }],
    unneeded: ['R'] };
  const out = L.layoutToDraft(draft, applied, result, '2026-10-01T15:00-07:00');
  const by = (n) => out.doc.receivers.find((r) => r.name === n);
  assert.equal(by('P').x, 4); assert.equal(by('P').room, 'b'); near(by('P').height, 0.4953);
  assert.deepEqual(by('P').moves, [{ at: '2026-10-01T15:00-07:00', floor: 'f0', room: 'a', x: 1, y: 1, height: 0.3 }]);
  assert.deepEqual(by('Q'), applied.receivers[1]);                               // stays where it is
  assert.deepEqual(by('R'), applied.receivers[2]);                               // not needed: left alone
  const n = out.doc.receivers.find((r) => !['P', 'Q', 'R'].includes(r.name));
  assert.equal(n.name, 'New proxy (A)'); assert.equal(n.x, 0.2); near(n.height, 1.2065); assert.deepEqual(n.moves, []);
  assert.equal(out.doc.floors[0].rooms[1].name, 'B renamed');
  assert.deepEqual(out.moved, ['P']); assert.deepEqual(out.added, ['New proxy (A)']); assert.deepEqual(out.missing, ['Gone']);
  assert.equal(draft.receivers[0].x, 1);                                         // the input draft is not changed
  const again = L.layoutToDraft(out.doc, applied, { moves: [result.moves[2]] }, 'later');
  assert.ok(again.doc.receivers.some((r) => r.name === 'New proxy (A) 2'));      // a second new one gets its own name
});

test('a suggested layout finds its floors by id, so a floor added in the draft does not shift them', () => {
  const rx = { id: 'p', name: 'P', floor: 'f0', room: 'a', x: 1, y: 1, height: 0.3, epochs: [], moves: [] };
  const applied = { ...doc(), receivers: [rx] };
  const draft = L.addFloor(JSON.parse(JSON.stringify(applied)), 'below', 0);         // f0 is now index 1
  const fi = draft.floors.findIndex((f) => f.id === 'f0');
  assert.equal(fi, 1);
  const out = L.layoutToDraft(draft, applied, { moves: [
    { candidate: 'out:o1', receiver: 'P', floor: 0, floor_id: 'f0', x: 4, y: 0.2, room: 'B', height: 0.5 },
    { candidate: 'out:o2', receiver: null, floor: 0, floor_id: 'gone', x: 1, y: 1, room: 'A', height: 0.5 }] }, 'now');
  const p = out.doc.receivers.find((r) => r.name === 'P');
  assert.equal(p.floor, 'f0'); assert.equal(p.room, 'b');
  assert.deepEqual(out.added, []); assert.deepEqual(out.missing, ['out:o2']);      // a floor the draft no longer has
});

test('in setup mode Apply says the engine restarts, not that tracking pauses (A3 final review)', () => {
  const s = L.applyCopy(true), t = L.applyCopy(false);
  for (const v of Object.values(s)) assert.ok(!/[Tt]racking/.test(v), v);
  assert.equal(t.confirm, 'Apply now? Tracking pauses ~1 min');
  assert.ok(/restarts/.test(s.wait) && s.confirm === 'Apply now?');
});
