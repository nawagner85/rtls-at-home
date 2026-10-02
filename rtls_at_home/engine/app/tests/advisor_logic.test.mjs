// node --test app/tests/advisor_logic.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import * as A from '../rtls/static/advisor_logic.js';

test('progress reads as what the advisor is doing', () => {
  assert.equal(A.stepText(null), 'Starting…');
  assert.equal(A.stepText({ step: 'prepare' }), 'Loading the live fit…');
  assert.equal(A.stepText({ step: 'geometry', candidates: 37 }), 'Working out the reach from 37 spots…');
  assert.equal(A.stepText({ step: 'truth', points: 567, rooms: 30 }), 'Simulating 567 phone positions in 30 rooms…');
  assert.equal(A.stepText({ step: 'today', score: [0.41, 0.9] }), "Today's layout: room 41%, floor 90%. Choosing outlets…");
  assert.equal(A.stepText({ step: 'greedy', k: 3, of: 9 }), 'Choosing spot 3 of 9…');
  assert.equal(A.stepText({ step: 'swaps', round: 2, at: 4, of: 7, score: [0.55, 0.95] }),
    'Trying swaps (round 2, spot 4 of 7): room 55%, floor 95%');
});

test('the moves read as who goes where, new proxies and the ones not needed last', () => {
  const r = { moves: [
    { candidate: 'out:o3', receiver: 'Hall Proxy', outlet: 'o3', floor: 1, room: 'Kitchen' },
    { candidate: 'now:Loft Proxy', receiver: 'Loft Proxy', outlet: null, floor: 0, room: 'Bonus Room' },
    { candidate: 'out:o9', receiver: null, outlet: 'o9', floor: 2, room: 'Loft' }],
    unneeded: ['Shed Proxy'] };
  assert.deepEqual(A.moveRows(r, ['Lower', 'Main', 'Upper']), [
    { who: 'Hall Proxy', what: 'move', where: 'Kitchen · Main', outlet: 'o3', floor: 1 },
    { who: 'Loft Proxy', what: 'stays', where: 'Bonus Room · Lower', outlet: null, floor: 0 },
    { who: 'New proxy', what: 'new', where: 'Loft · Upper', outlet: 'o9', floor: 2 },
    { who: 'Shed Proxy', what: 'unplug', where: '', outlet: null, floor: null }]);
  assert.deepEqual(A.suggestedOutlets(r), [{ id: 'o3', label: 'Hall Proxy' }, { id: 'o9', label: 'new' }]);
});

test('the curve shows what more proxies would add', () => {
  const r = { request: { proxies: 2 }, suggested: { room: 0.6, floor: 0.95 },
              curve: [{ n: 1, room: 0.4, floor: 0.9 }, { n: 2, room: 0.58, floor: 0.94 }, { n: 3, room: 0.66, floor: 0.97 },
                      { n: 4, room: 0.7, floor: 0.97 }] };
  assert.deepEqual(A.moreRows(r.curve, 2), [{ n: 3, room: '66%', floor: '97%', gain: '+8' }, { n: 4, room: '70%', floor: '97%', gain: '+4' }]);
  assert.equal(A.pct(0.4132), '41%');
});

test('"Use this layout" refuses while the editor has edits it could not save, and writes nothing', async () => {
  const calls = [];
  const io = (saved) => ({
    settle: async () => saved,
    getDraft: async () => { calls.push('get'); return { doc: { floors: [{ id: 'f0', name: 'G', rooms: [] }], receivers: [] } }; },
    getApplied: async () => ({ floors: [], receivers: [] }),
    putDraft: async (d) => { calls.push('put'); },
    reload: async () => { calls.push('reload'); },
    now: () => 'now',
  });
  const result = { moves: [{ candidate: 'out:o1', receiver: null, floor: 0, floor_id: 'f0', x: 1, y: 1, room: 'A', height: 0.5 }] };
  await assert.rejects(A.useLayout(io(false), result), /not saved/);
  assert.deepEqual(calls, []);
  const out = await A.useLayout(io(true), result);
  assert.deepEqual(calls, ['get', 'put', 'reload']); assert.deepEqual(out.added, ['New proxy (A)']);
});

// ---------- taking part of a suggestion (spec section 4) ----------
const R4 = { moves: [
  { candidate: 'now:Hall', receiver: 'Hall', outlet: 'h1', floor: 0 },          // row 0: stays
  { candidate: 'out:o2', receiver: 'Office', outlet: 'o2', floor: 1 },           // row 1: moves
  { candidate: 'out:o3', receiver: 'Dining', outlet: 'o3', floor: 1 },           // row 2: moves
  { candidate: 'out:o9', receiver: null, outlet: 'o9', floor: 2 }],              // row 3: new
  unneeded: ['Garage'] };                                                         // row 4: not needed
const all = new Set([0, 1, 2, 3, 4]);

test('a selection is the ticked targets, the unticked proxies where they are, no unticked new ones', () => {
  assert.deepEqual(A.selectionKeys(R4, all), ['now:Hall', 'out:o2', 'out:o3', 'out:o9']);
  assert.deepEqual(A.selectionKeys(R4, new Set([0, 2])), ['now:Hall', 'now:Office', 'out:o3', 'now:Garage']);
  assert.deepEqual(A.selectionKeys(R4, new Set()), ['now:Hall', 'now:Office', 'now:Dining', 'now:Garage']);   // today
  assert.equal(A.isPartial(R4, all), false); assert.equal(A.isPartial(R4, new Set([0, 1, 2, 3])), true);
});

test('using a selection writes the ticked moves only', () => {
  assert.deepEqual(A.selectedResult(R4, new Set([2, 4])).moves.map((m) => m.candidate), ['out:o3']);
  assert.deepEqual(A.selectedResult(R4, all).moves, R4.moves);
  assert.equal(A.changesIn(A.selectedResult(R4, new Set([0, 4]))), 0);         // a stay and an unplug change nothing on the map
});

test('re-running around a selection keeps the unticked proxies and asks for the rest', () => {
  const req = { proxies: 4, fixed: ['Den Echo Show 8'] };
  assert.deepEqual(A.rerunRequest(req, R4, new Set([0, 2])),                    // Office and the new one unticked, Garage kept
    { proxies: 2, fixed: ['Den Echo Show 8', 'Office', 'Garage'] });
  assert.throws(() => A.rerunRequest(req, R4, new Set([4])), /at least one/);
});
