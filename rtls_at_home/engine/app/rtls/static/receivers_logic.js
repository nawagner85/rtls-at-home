// Receivers page's pure logic (Nick 2026-09-27: turn specific proxies and Echo Shows on and off). No DOM - tested by
// app/tests/receivers_logic.test.mjs; static/receivers.js draws with it.

// Top floor first (the way the house stacks), then by name.
export function sortReceivers(rows) {
  return rows.slice().sort((a, b) => (b.floor ?? -1) - (a.floor ?? -1) || String(a.name).localeCompare(String(b.name)));
}

export const statusText = (r) => (!r.enabled ? 'off' : !r.alive ? 'not heard' : 'live');

// The live anchor correction applied to this receiver's expected level, when it is big enough to matter.
export function corrText(r) {
  const c = r.correction;
  if (c == null || Math.abs(c) < 0.5) return '';
  return `${c < 0 ? '−' : '+'}${Math.abs(c).toFixed(1)} dB`;
}

export function summaryText(rows) {
  const on = rows.filter(r => r.enabled).length, quiet = rows.filter(r => r.enabled && !r.alive).length;
  return `${on} of ${rows.length} on` + (quiet ? `, ${quiet} not heard` : '');
}
