// node --test app/tests/
import test from 'node:test';
import assert from 'node:assert/strict';
import { sortReceivers, statusText, corrText, summaryText } from '../rtls/static/receivers_logic.js';

const R = (name, floor, extra = {}) => ({ name, floor, floor_name: ['Floor 1', 'Main', 'Floor 3'][floor], kind: 'proxy',
                                          enabled: true, alive: true, room: 'Office', correction: 0, level: 0, ...extra });

test('receivers are listed top floor first, then by name', () => {
  const rows = [R('Shed Proxy', 0), R('Hall Proxy', 1), R('Annex Proxy', 2), R('Study Proxy', 1)];
  assert.deepEqual(sortReceivers(rows).map(r => r.name), ['Annex Proxy', 'Hall Proxy', 'Study Proxy', 'Shed Proxy']);
  assert.deepEqual(rows.map(r => r.name)[0], 'Shed Proxy');                 // the input is not reordered
});

test('status: off wins, then not heard, else live', () => {
  assert.equal(statusText(R('a', 1, { enabled: false, alive: false })), 'off');
  assert.equal(statusText(R('a', 1, { alive: false })), 'not heard');
  assert.equal(statusText(R('a', 1)), 'live');
});

test('a correction is shown when it is at least half a dB, with its sign', () => {
  assert.equal(corrText(R('a', 1, { correction: -7.04 })), '−7.0 dB');
  assert.equal(corrText(R('a', 1, { correction: 1.5 })), '+1.5 dB');
  assert.equal(corrText(R('a', 1, { correction: 0.3 })), '');
  assert.equal(corrText(R('a', 1, { correction: null })), '');
});

test('the summary counts what is on and what is not heard', () => {
  assert.equal(summaryText([R('a', 1), R('b', 1, { enabled: false }), R('c', 1, { alive: false })]),
               '2 of 3 on, 1 not heard');
  assert.equal(summaryText([R('a', 1), R('b', 1)]), '2 of 2 on');
});
