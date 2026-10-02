// Outlets mode's pure logic (outlet survey, Nick 2026-09-25): snapping a tap to a wall, nudging, colours, list order.
// No DOM, no three.js - tested by app/tests/outlets_logic.test.mjs; static/outlets.js draws with it.
const M_PER_IN = 0.0254;

// n: calib_geom.nearestXY's result - the nearest wall face along each axis ({side, face, d} or null). An outlet sits
// on ONE wall: the nearer of the two, `offset` off its face, if it is within `radius`; otherwise nothing.
export function snapToWall(n, x, y, offset, radius) {
  const cands = [['x', n.x], ['y', n.y]].filter(([, w]) => w && w.d <= radius);
  if (!cands.length) return null;
  const [ax, w] = cands.reduce((a, b) => (b[1].d < a[1].d ? b : a));
  const into = (w.side === 'east' || w.side === 'north') ? -offset : offset;   // step back off the face, into the room
  const r = (v) => Math.round(v * 1000) / 1000;
  return ax === 'x' ? { x: r(w.face + into), y, side: w.side } : { x, y: r(w.face + into), side: w.side };
}

// Arrow keys in map directions (the top view has north up): 5 cm, or 25 cm with shift.
export function nudgeXY(x, y, key, big) {
  const d = big ? 0.25 : 0.05;
  const step = { ArrowUp: [0, d], ArrowDown: [0, -d], ArrowLeft: [-d, 0], ArrowRight: [d, 0] }[key];
  return step ? { x: x + step[0], y: y + step[1] } : null;
}

// Free-text statuses from the candidate file, grouped for the marker colour.
export function statusKind(s) {
  const t = String(s || '').toLowerCase();
  if (t.startsWith('current')) return 'current';
  if (t.includes('deaf')) return 'deaf';
  if (t.startsWith('previous')) return 'previous';
  return 'candidate';
}

// By room, placed outlets before unplaced ones, then by id.
export function sortOutlets(list) {
  const placed = (o) => (o.x == null || o.y == null ? 1 : 0);
  return list.slice().sort((a, b) => String(a.room || '').localeCompare(String(b.room || '')) ||
    placed(a) - placed(b) || String(a.id).localeCompare(String(b.id), undefined, { numeric: true }));
}

export const heightText = (o) => `${o.height} ${Math.round((o.z_rel ?? 0) / M_PER_IN)}"`;

// Arrow-key nudges move the marker at once; the save goes out once the keys stop, with the latest position only -
// firing a save per key raced (three quick presses read the same stale position and moved 5 cm, not 15).
export function createLatestSender({ send, schedule, cancel, delay }) {
  const pending = {}, timers = {};
  return {
    push(id, body) {
      pending[id] = body;
      cancel(timers[id]);
      timers[id] = schedule(() => { const b = pending[id]; delete pending[id]; delete timers[id]; send(id, b); }, delay);
    },
  };
}

export const clip = (s, n) => { const t = String(s ?? ''); return t.length > n ? t.slice(0, n - 1) + '…' : t; };
