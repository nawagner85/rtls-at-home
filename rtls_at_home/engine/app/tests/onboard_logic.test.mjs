// node --test app/tests/
import test from 'node:test';
import assert from 'node:assert/strict';
import { filterCandidates, sortCandidates, isNew, isRotating, whereText, hiddenText, stableOrder, assignNumbers, createPoller,
         identityText, riseText, RISE_DB }
  from '../rtls/static/onboard_logic.js';

const C = (key, outside, extra = {}) => ({ key, outside, room: 'Office', floor_name: 'Main',
                                            rssi: -70, name: null, first_seen: null, addr_type: 'public', ...extra });

test('devices that are probably outside are hidden unless asked for, and counted', () => {
  const all = [C('a', false), C('b', true), C('c', false), C('d', true)];
  let r = filterCandidates(all, false);
  assert.deepEqual(r.vis.map(c => c.key), ['a', 'c']);
  assert.equal(r.far, 2);
  r = filterCandidates(all, true);
  assert.deepEqual(r.vis.map(c => c.key), ['a', 'b', 'c', 'd']);
  assert.equal(r.far, 0);
  assert.equal(filterCandidates([], false).far, 0);
});

test('sorting: newest first, loudest first, or by name with unnamed devices last', () => {
  const list = [C('a', false, { first_seen: 100, rssi: -80, name: 'Zed' }), C('b', false, { first_seen: 300, rssi: -60 }),
                C('c', false, { first_seen: null, rssi: -70, name: 'Alpha' })];
  assert.deepEqual(sortCandidates(list, 'new').map(c => c.key), ['b', 'a', 'c']);
  assert.deepEqual(sortCandidates(list, 'loud').map(c => c.key), ['b', 'c', 'a']);
  assert.deepEqual(sortCandidates(list, 'name').map(c => c.key), ['c', 'a', 'b']);
  assert.deepEqual(list.map(c => c.key), ['a', 'b', 'c']);           // the input is not reordered
});

test('new means first heard in the last 10 minutes', () => {
  assert.equal(isNew({ first_seen: 1000 }, 1000 + 599), true);
  assert.equal(isNew({ first_seen: 1000 }, 1000 + 601), false);
  assert.equal(isNew({ first_seen: null }, 1000), false);
});

test('rotating addresses are the resolvable and non-resolvable random kinds', () => {
  assert.equal(isRotating({ addr_type: 'random_resolvable' }), true);
  assert.equal(isRotating({ addr_type: 'random_nonresolvable' }), true);
  for (const t of ['public', 'random_static', null, undefined]) assert.equal(isRotating({ addr_type: t }), false);
});

test('where: the room and floor, or "probably outside"', () => {
  assert.equal(whereText(C('a', false)), 'Office · Main');
  assert.equal(whereText(C('a', true)), 'probably outside');
  assert.equal(whereText({ ...C('a', false), room: null }), '— · Main');
});

test('the hidden line counts every reason', () => {
  assert.equal(hiddenText(31, 7, 120), '158 hidden: 31 probably outside, 7 heard by one proxy only, ' +
                                       '120 phones, watches and laptops (rotating addresses)');
  assert.equal(hiddenText(0, 0, 0), '0 hidden: 0 probably outside, 0 heard by one proxy only, ' +
                                    '0 phones, watches and laptops (rotating addresses)');
});

test('rotating addresses are hidden unless asked for; an iBeacon on one is not rotating: its key is stable', () => {
  const rot = { addr_type: 'random_resolvable' };
  const all = [C('a', false), C('b', false, rot), C('c', true, rot), C('d', false, { ...rot, ibeacon: true })];
  let r = filterCandidates(all, false, false);
  assert.deepEqual(r.vis.map(c => c.key), ['a', 'd']);
  assert.equal(r.far, 1); assert.equal(r.rotating, 1);            // c counts as outside, once
  r = filterCandidates(all, false, true);
  assert.deepEqual(r.vis.map(c => c.key), ['a', 'b', 'd']);
  assert.equal(r.rotating, 0);
  assert.equal(isRotating({ addr_type: 'random_resolvable', ibeacon: true }), false);
});

test('sorting by rising puts the device just held next to a proxy first', () => {
  const list = [C('a', false, { rise: { db: 3, proxy: 'Shed Proxy' } }), C('b', false, { rise: null }),
                C('c', false, { rise: { db: 27, proxy: 'Hall Proxy' } })];
  assert.deepEqual(sortCandidates(list, 'rise').map(c => c.key), ['c', 'a', 'b']);
});

test('what it probably is, and how much it just rose where', () => {
  assert.equal(identityText({ vendor: 'Apple', kind: 'Find My beacon (AirTag or another Find My device)' }),
               'Apple · Find My beacon (AirTag or another Find My device)');
  assert.equal(identityText({ vendor: 'Sonos Inc', kind: null }), 'Sonos Inc');
  assert.equal(identityText({ vendor: null, kind: 'iBeacon' }), 'iBeacon');
  assert.equal(identityText({}), '');
  assert.equal(riseText({ rise: { db: 26.6, proxy: 'Hall Proxy' } }), '↑ 27 dB at Hall Proxy');
  assert.equal(riseText({ rise: { db: RISE_DB - 1, proxy: 'Hall Proxy' } }), '');
  assert.equal(riseText({ rise: null }), '');
});

test('the list keeps its order between updates; new devices join at the end; a re-sort reorders everything', () => {
  const a = C('a', false, { rssi: -80 }), b = C('b', false, { rssi: -60 }), c = C('c', false, { rssi: -70 });
  let order = stableOrder([], [a, b], 'loud', false);                  // first view: sorted
  assert.deepEqual(order, ['b', 'a']);
  const a2 = { ...a, rssi: -50 };                                       // a got louder: no reshuffle
  order = stableOrder(order, [a2, b, c], 'loud', false);
  assert.deepEqual(order, ['b', 'a', 'c']);                             // c is new: appended
  order = stableOrder(order, [a2, c], 'loud', false);                   // b went away
  assert.deepEqual(order, ['a', 'c']);
  assert.deepEqual(stableOrder(order, [a2, b, c], 'loud', true), ['a', 'b', 'c']);   // Refresh / sort pressed
});

test('each device keeps its number for the session', () => {
  const nums = {};
  assignNumbers(nums, ['x', 'y']);
  assert.deepEqual(nums, { x: 1, y: 2 });
  assignNumbers(nums, ['z', 'x']);
  assert.deepEqual(nums, { x: 1, y: 2, z: 3 });                         // y gone from view keeps its number
});

test('a poll in flight when the panel closes does nothing when it lands (final review, Important 2)', async () => {
  let active = true, resolve, applied = 0;
  const timers = [];
  const p = createPoller({ fetch: () => new Promise(r => { resolve = r; }), apply: () => { applied++; },
                           isActive: () => active, schedule: (fn) => { timers.push(fn); return timers.length; },
                           cancel: () => {}, every: 10000 });
  p.start();
  active = false; p.stop();                                   // the user leaves Onboard mode mid-request
  resolve({ status: 'ready' }); await new Promise(r => setTimeout(r, 0));
  assert.equal(applied, 0);
  assert.equal(timers.length, 0);                             // and no new timer: the chain ends
});

test('starting twice keeps a single polling chain', async () => {
  const resolvers = [], timers = [];
  let applied = 0;
  const p = createPoller({ fetch: () => new Promise(r => resolvers.push(r)), apply: () => { applied++; },
                           isActive: () => true, schedule: (fn) => { timers.push(fn); return timers.length; },
                           cancel: () => {}, every: 10000 });
  p.start(); p.start();                                       // Onboard clicked twice
  resolvers.forEach(r => r({})); await new Promise(r => setTimeout(r, 0));
  assert.equal(applied, 1);
  assert.equal(timers.length, 1);
});
