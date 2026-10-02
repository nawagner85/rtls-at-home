// Onboarding's pure logic (spec 3.4): the distance filter, sorting and the labels. No DOM, no three.js - tested by
// app/tests/onboard_logic.test.mjs; static/onboard.js draws with it.
export const NEW_S = 600;                   // a device first heard within 10 minutes is "new"
export const RISE_DB = 10;                  // a rise this big since the last update is worth pointing out

// Distance outside the house can't be measured with this setup (2026-09-25); the locator says "probably outside"
// instead, and those devices are hidden unless asked for.
// Phones, watches and laptops change their address every few minutes, so they can't be added (unless they send an
// iBeacon, which has a stable key): hidden unless asked for, and counted apart (Nick 2026-09-27).
export function filterCandidates(all, showOutside, showRotating = false) {
  const near = showOutside ? all.slice() : all.filter(c => !c.outside);
  const vis = showRotating ? near : near.filter(c => !isRotating(c));
  return { vis, far: all.length - near.length, rotating: near.length - vis.length };
}

export function sortCandidates(list, by) {
  const cmp = {
    new: (a, b) => (b.first_seen || 0) - (a.first_seen || 0),
    loud: (a, b) => (b.rssi ?? -999) - (a.rssi ?? -999),
    name: (a, b) => (a.name ? 0 : 1) - (b.name ? 0 : 1) || String(a.name || '').localeCompare(String(b.name || '')),
    rise: (a, b) => (b.rise?.db ?? -999) - (a.rise?.db ?? -999),
  }[by];
  return list.slice().sort(cmp);
}

export const isNew = (c, nowSec) => !!c.first_seen && nowSec - c.first_seen < NEW_S;
export const isRotating = (c) => !c.ibeacon && (c.addr_type === 'random_resolvable' || c.addr_type === 'random_nonresolvable');

// What it probably is (engine identify.py: maker from the Bluetooth ids or a public address prefix, kind from the
// message type or service), and how much it just rose at one proxy - hold a device next to a proxy to find it.
export const identityText = (c) => [c.vendor, c.kind].filter(Boolean).join(' · ');
export const riseText = (c) => (c.rise && c.rise.db >= RISE_DB) ? `↑ ${Math.round(c.rise.db)} dB at ${c.rise.proxy}` : '';

export function whereText(c) {
  return c.outside ? 'probably outside' : `${c.room || '—'} · ${c.floor_name}`;
}

export const hiddenText = (far, one, rot = 0) => `${far + one + rot} hidden: ${far} probably outside, ` +
  `${one} heard by one proxy only, ${rot} phones, watches and laptops (rotating addresses)`;

// The list must not reshuffle under the cursor (Nick 2026-09-25): keep the previous order for devices still here,
// add new ones at the end (sorted among themselves), and re-sort everything only when asked.
export function stableOrder(prev, cands, by, resort) {
  const here = new Set(cands.map(c => c.key));
  if (resort || !prev.length) return sortCandidates(cands, by).map(c => c.key);
  const kept = prev.filter(k => here.has(k)), old = new Set(kept);
  return kept.concat(sortCandidates(cands.filter(c => !old.has(c.key)), by).map(c => c.key));
}

// A device keeps its number (and its ring's label) for as long as the panel is open.
export function assignNumbers(nums, keys) {
  let next = Math.max(0, ...Object.values(nums)) + 1;
  for (const k of keys) if (!(k in nums)) nums[k] = next++;
  return nums;
}

// Poll while the panel is open; a request still in flight when the panel closes (or reopens) must not render or
// schedule another poll - otherwise the lease never lapses and the bridge keeps sending (final review, Important 2).
export function createPoller({ fetch, apply, isActive, schedule, cancel, every }) {
  let gen = 0, timer = null;
  async function tick(g) {
    let data;
    try { data = await fetch(); } catch (e) { data = { status: 'error', error: e.message, candidates: [], one_proxy: 0 }; }
    if (g !== gen || !isActive()) return;
    apply(data);
    timer = schedule(() => tick(g), every);
  }
  return {
    start() { gen++; cancel(timer); timer = null; tick(gen); },
    stop() { gen++; cancel(timer); timer = null; },
  };
}
