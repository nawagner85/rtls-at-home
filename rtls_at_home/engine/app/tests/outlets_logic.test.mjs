// node --test app/tests/
import test from 'node:test';
import assert from 'node:assert/strict';
import { snapToWall, nudgeXY, statusKind, sortOutlets, heightText, createLatestSender, clip } from '../rtls/static/outlets_logic.js';

// nearestXY's shape (calib_geom.js): the nearest wall face on each axis, or null
const N = (x, y) => ({ x, y });

test('an outlet snaps to the single nearest wall face, a few cm off it', () => {
  const n = N({ side: 'west', face: 1.0, d: 0.20 }, { side: 'north', face: 5.0, d: 0.40 });
  assert.deepEqual(snapToWall(n, 1.2, 4.6, 0.03, 0.6), { x: 1.03, y: 4.6, side: 'west' });
  const m = N({ side: 'east', face: 3.0, d: 0.50 }, { side: 'south', face: 2.0, d: 0.10 });
  assert.deepEqual(snapToWall(m, 2.5, 2.1, 0.03, 0.6), { x: 2.5, y: 2.03, side: 'south' });
});

test('a tap with no wall within reach places nothing', () => {
  assert.equal(snapToWall(N({ side: 'west', face: 0, d: 0.9 }, null), 0.9, 3, 0.03, 0.6), null);
  assert.equal(snapToWall(N(null, null), 5, 5, 0.03, 0.6), null);
});

test('arrow keys nudge 5 cm (25 cm with shift) in map directions; other keys do nothing', () => {
  const r = nudgeXY(1, 1, 'ArrowUp', false);
  assert.ok(Math.abs(r.y - 1.05) < 1e-9 && r.x === 1);
  assert.ok(Math.abs(nudgeXY(1, 1, 'ArrowLeft', true).x - 0.75) < 1e-9);
  assert.equal(nudgeXY(1, 1, 'Enter', false), null);
});

test('statuses group into current / deaf / previous / candidate for the marker colour', () => {
  assert.equal(statusKind('current: Hall Proxy (since 2026-09-24 20:24)'), 'current');
  assert.equal(statusKind('tested: deaf (Nick)'), 'deaf');
  assert.equal(statusKind('previous Loft Proxy spot (until 2026-09-22)'), 'previous');
  assert.equal(statusKind('candidate'), 'candidate');
  assert.equal(statusKind(undefined), 'candidate');
});

test('the list runs by room, placed before unplaced, and heights read in inches', () => {
  const L = [{ id: 'o2', room: 'Office', x: 1, y: 1 }, { id: 'a', room: 'Bathroom', x: 1, y: 1 },
             { id: 'u', room: 'Office', x: null, y: null }, { id: 'o1', room: 'Office', x: 2, y: 2 }];
  assert.deepEqual(sortOutlets(L).map(o => o.id), ['a', 'o1', 'o2', 'u']);
  assert.equal(heightText({ height: 'standard', z_rel: 0.4064 }), 'standard 16"');
  assert.equal(heightText({ height: 'counter', z_rel: 1.1176 }), 'counter 44"');
  assert.equal(heightText({ height: 'measured', z_rel: 1.6 }), 'measured 63"');
});

test('quick nudges send one save with the final position (latest wins), after a pause', () => {
  const sent = [], timers = [];
  const q = createLatestSender({ send: (id, body) => sent.push([id, body]), schedule: (fn) => { timers.push(fn); return timers.length; },
                                 cancel: (t) => { if (t) timers[t - 1] = null; }, delay: 300 });
  q.push('o1', { x: 1.05, y: 2 }); q.push('o1', { x: 1.10, y: 2 }); q.push('o1', { x: 1.15, y: 2 });
  assert.equal(sent.length, 0);
  timers.filter(Boolean).forEach(fn => fn());
  assert.deepEqual(sent, [['o1', { x: 1.15, y: 2 }]]);
});

test('long notes are clipped in the list, whole in the editor', () => {
  assert.equal(clip('short', 60), 'short');
  assert.equal(clip('x'.repeat(70), 60), 'x'.repeat(59) + '…');
  assert.equal(clip(null, 60), '');
});
