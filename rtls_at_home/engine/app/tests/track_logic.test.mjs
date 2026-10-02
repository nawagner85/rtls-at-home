// node --test app/tests/
// The production tracking panel (Nick 2026-09-26): devices are listed, hidden or focused; nothing here touches the DOM.
import test from 'node:test';
import assert from 'node:assert/strict';
import { FLAGS, toggleHidden, visibleKeys, rowModel, loadHidden, saveHidden, deviceStatus } from '../rtls/static/track_logic.js';

test('the experiments are off by default and DominantPath is the model shown', () => {
  assert.equal(FLAGS.models, false);          // the FreeSpace / DominantPath choice stays in code, not on the page
  assert.equal(FLAGS.heat, 'DominantPath');
  assert.equal(FLAGS.groundTruth, false);     // "I'm here" is deprecated
  assert.equal(FLAGS.bermuda, false);
  assert.equal(FLAGS.signal, false);          // the half-life / filter controls
});

test('hiding a device is a per-viewer set that toggles', () => {
  const h1 = toggleHidden(new Set(), 'a');
  assert.deepEqual([...h1], ['a']);
  assert.deepEqual([...toggleHidden(h1, 'a')], []);
  assert.deepEqual([...toggleHidden(h1, 'b')].sort(), ['a', 'b']);
});

test('the map shows every device but the hidden ones, or only the focused one', () => {
  const keys = ['a', 'b', 'c'];
  assert.deepEqual(visibleKeys(keys, new Set(['b']), null), ['a', 'c']);
  assert.deepEqual(visibleKeys(keys, new Set(['b']), 'c'), ['c']);
  assert.deepEqual(visibleKeys(keys, new Set(['c']), 'c'), ['c']);   // focusing a hidden device still shows it
});

test('a row reads name + where, with floor, confidence and battery when the device reports one', () => {
  const r = { key: 'a', label: 'Tag 1 (FSC-BP106)', seen: true, est: { room: 'Living Room', floor_name: 'Main', p_room: 0.71 } };
  const m = rowModel(r, { name: 'Tag 1 (FSC-BP106)', kind: 'tag' }, new Set());
  assert.equal(m.name, 'Tag 1');
  assert.equal(m.where, 'Living Room');
  assert.equal(m.detail, 'Main · 71%');
  assert.equal(m.dim, false);
  const b = rowModel({ ...r, battery: 87 }, { name: 'Tag 1', kind: 'tag' }, new Set());
  assert.equal(b.detail, 'Main · 71% · 87% battery');
});

test('an away or hidden device reads dimmed, a never-heard one says so', () => {
  const away = rowModel({ key: 'a', label: 'Rex', status: 'away', away_text: 'away since 8:29 pm' }, { name: 'Rex', kind: 'phone' }, new Set());
  assert.equal(away.where, 'away since 8:29 pm'); assert.equal(away.dim, true);
  const hid = rowModel({ key: 'a', label: 'Tag 2', seen: true, est: { room: 'Garage', floor_name: 'Floor 1', p_room: 0.5 } }, { name: 'Tag 2', kind: 'tag' }, new Set(['a']));
  assert.equal(hid.dim, true); assert.equal(hid.hidden, true);
  const quiet = rowModel({ key: 'a', label: 'Tag 3', seen: false, age: 125 }, { name: 'Tag 3', kind: 'tag' }, new Set());
  assert.equal(quiet.where, 'not heard for 2 min');
});

test('the hidden set survives in storage and tolerates a broken store', () => {
  const store = { v: null, getItem() { return this.v; }, setItem(k, v) { this.v = v; } };
  saveHidden(new Set(['a', 'b']), store);
  assert.deepEqual([...loadHidden(store)].sort(), ['a', 'b']);
  const broken = { getItem() { throw new Error('private'); }, setItem() { throw new Error('private'); } };
  assert.deepEqual([...loadHidden(broken)], []);
  saveHidden(new Set(['a']), broken);                                   // no throw
});

test('the status row says a new household has no device yet instead of a red "not heard" (A3 final review)', () => {
  assert.deepEqual(deviceStatus({ cfg: { device: null }, device_label: '?', seen: false }),
    { label: 'no device yet - add one in Setup → Onboard', pill: null });
  assert.deepEqual(deviceStatus({ cfg: { device: 'k' }, device_label: 'Keys', seen: false, device_age: 12.4 }),
    { label: 'Keys', pill: 'not heard for 12 s' });
  assert.deepEqual(deviceStatus({ cfg: { device: 'k' }, device_label: 'Keys', seen: true }), { label: 'Keys', pill: null });
});
