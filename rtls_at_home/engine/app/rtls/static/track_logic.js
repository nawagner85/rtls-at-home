// The production tracking panel, the pure parts (Nick 2026-09-26: "less diagnostic and more production").
// Devices are listed; each can be hidden on the map (this viewer only) or focused (the map shows it alone and
// flies to it). Experiments stay in the code behind FLAGS until a settings page exposes them.
// No DOM here - tested by app/tests/track_logic.test.mjs.

// Today's defaults for everything the page used to expose. A later settings page changes these; nothing else.
export const FLAGS = Object.freeze({
  models: false,            // the FreeSpace / DominantPath buttons and the estimates table
  heat: 'DominantPath',     // the posterior heat map drawn for the focused device
  trail: true,
  roomFilter: true,
  confidence: false,
  groundTruth: false,       // "I'm here" / "Label room" - the old calibration; deprecated
  bermuda: false,           // the Bermuda comparison line
  signal: false,            // half-life, signal filter, motion filter, the scanner table
  session: false,           // the recording line
});

export function toggleHidden(hidden, key) {
  const out = new Set(hidden);
  if (out.has(key)) out.delete(key); else out.add(key);
  return out;
}

// Which devices the map draws: only the focused one while focusing, else all but the hidden.
export function visibleKeys(keys, hidden, focus) {
  if (focus) return keys.filter(k => k === focus);
  return keys.filter(k => !hidden.has(k));
}

const shortName = (label) => String(label ?? '').replace(/\s*\(.*\)\s*$/, '') || String(label ?? '');
const pct = (p) => (p == null ? '' : Math.round(100 * p) + '%');
const ago = (s) => (s < 90 ? Math.round(s) + ' s' : s < 5400 ? Math.round(s / 60) + ' min' : Math.round(s / 3600) + ' h');

// One row: {name, where, detail, dim, hidden}. `detail` grows as devices report more (battery today).
export function rowModel(r, dev, hidden) {
  const name = shortName(dev?.name || r.label);
  const isHidden = hidden.has(r.key);
  let where, detail = '';
  if (r.status === 'away') where = r.away_text || 'away';
  else if (r.est) {
    where = r.est.room;
    detail = [r.est.floor_name, pct(r.est.p_room)].filter(Boolean).join(' · ');
  } else where = r.seen ? 'locating…' : 'not heard' + (r.age != null ? ' for ' + ago(r.age) : '');
  if (r.battery != null) detail = (detail ? detail + ' · ' : '') + Math.round(r.battery) + '% battery';
  return { name, where, detail, dim: isHidden || r.status === 'away' || !r.seen, hidden: isHidden };
}

const KEY = 'hiddenDevices';
export function loadHidden(store) {
  try { return new Set(JSON.parse(store.getItem(KEY) || '[]')); } catch (e) { return new Set(); }
}
export function saveHidden(hidden, store) {
  try { store.setItem(KEY, JSON.stringify([...hidden])); } catch (e) { /* private window, blocked storage */ }
}

// The status row's device part. A new household has no device until one is onboarded: say so, neutrally, instead of
// a red "not heard" for a device that does not exist (A3 final review).
export function deviceStatus(state) {
  if (!state.cfg || state.cfg.device == null) return { label: 'no device yet - add one in Setup → Onboard', pill: null };
  return { label: state.device_label,
           pill: state.seen ? null : 'not heard' + (state.device_age != null ? ` for ${Math.round(state.device_age)} s` : '') };
}
