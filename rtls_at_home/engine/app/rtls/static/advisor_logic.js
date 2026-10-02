// The placement advisor's words (spec docs/specs/2026-10-01-placement-advisor-design.md): progress, the moves, and
// what more proxies would add. Pure, for advisor.js; tested with node --test app/tests/advisor_logic.test.mjs.
import { layoutToDraft } from './map_logic.js';

export const pct = (x) => `${Math.round(x * 100)}%`;

export function stepText(p) {
  if (!p || !p.step) return 'Starting…';
  const sc = (s) => `room ${pct(s[0])}, floor ${pct(s[1])}`;
  switch (p.step) {
    case 'prepare': return 'Loading the live fit…';
    case 'geometry': return `Working out the reach from ${p.candidates} spots…`;
    case 'truth': return `Simulating ${p.points} phone positions in ${p.rooms} rooms…`;
    case 'today': return `Today's layout: ${sc(p.score)}. Choosing outlets…`;
    case 'greedy': return `Choosing spot ${p.k} of ${p.of}…`;
    case 'swaps': return `Trying swaps (round ${p.round}, spot ${p.at} of ${p.of}): ${sc(p.score)}`;
    default: return `${p.step}…`;
  }
}

// Who goes where: the proxies that move, stay or are new, then the ones not needed (unplug).
export function moveRows(result, floorNames) {
  const where = (m) => [m.room, floorNames[m.floor]].filter(Boolean).join(' · ');
  const rows = ((result && result.moves) || []).map((m) => ({
    who: m.receiver || 'New proxy',
    what: !m.receiver ? 'new' : String(m.candidate).startsWith('now:') ? 'stays' : 'move',
    where: where(m), outlet: m.outlet ?? null, floor: m.floor ?? null,
  }));
  return rows.concat(((result && result.unneeded) || []).map((who) => ({ who, what: 'unplug', where: '', outlet: null, floor: null })));
}

// The outlets of the suggestion, for the 3-D view: the proxy that goes there, or "new".
export const suggestedOutlets = (result) => ((result && result.moves) || [])
  .filter((m) => m.outlet).map((m) => ({ id: m.outlet, label: m.receiver || 'new' }));

// The greedy curve past n: each extra proxy's room and floor accuracy and the room points it adds.
export function moreRows(curve, n) {
  const by = new Map((curve || []).map((c) => [c.n, c]));
  return (curve || []).filter((c) => c.n > n).map((c) => ({
    n: c.n, room: pct(c.room), floor: pct(c.floor),
    gain: by.has(c.n - 1) ? `${c.room >= by.get(c.n - 1).room ? '+' : ''}${Math.round((c.room - by.get(c.n - 1).room) * 100)}` : '',
  }));
}

// "Use this layout": the editor's own edits saved first (refused while they cannot be - writing over the server's
// older draft would lose them), then the moves added to the draft as it is, written back and opened in the editor.
// io: { settle() -> saved?, getDraft(), getApplied(), putDraft(doc), reload(), now() }.
export async function useLayout(io, result) {
  if (!(await io.settle())) {
    throw new Error('The map has edits that are not saved yet: open the Map tab, let them save, then try again');
  }
  const [st, applied] = await Promise.all([io.getDraft(), io.getApplied()]);
  const out = layoutToDraft(st.doc, applied, result, io.now());
  await io.putDraft(out.doc);
  await io.reload();
  return out;
}

// ---------- taking part of a suggestion (spec section 4) ----------
// `ticked`: a Set of row indices over moveRows - the moves (in order), then the proxies not needed.
const unneededOf = (r) => (r && r.unneeded) || [];

// The spots of the ticked selection, to score: a ticked row's target, an unticked proxy where it is now, an unticked
// new proxy left out, an unticked "not needed" proxy kept where it is.
export function selectionKeys(result, ticked) {
  const keys = [];
  result.moves.forEach((m, i) => {
    if (ticked.has(i)) keys.push(m.candidate);
    else if (m.receiver) keys.push('now:' + m.receiver);
  });
  unneededOf(result).forEach((name, k) => { if (!ticked.has(result.moves.length + k)) keys.push('now:' + name); });
  return keys;
}

export const isPartial = (result, ticked) =>
  [...Array(result.moves.length + unneededOf(result).length).keys()].some((i) => !ticked.has(i));

// The result with the ticked moves only (what "Use this layout" writes).
export const selectedResult = (result, ticked) => ({ ...result, moves: result.moves.filter((_, i) => ticked.has(i)) });

// Map changes a result makes: moved and new proxies (a proxy staying on its own spot changes nothing).
export const changesIn = (result) => result.moves.filter((m) => !m.receiver || !String(m.candidate).startsWith('now:')).length;

// A new run around the selection: the unticked proxies stay where they are (with the receivers fixed before), and the
// number of proxies drops by the unticked rows that were proxies of the suggestion.
export function rerunRequest(request, result, ticked) {
  const fixed = [...request.fixed], add = (n) => { if (n && !fixed.includes(n)) fixed.push(n); };
  let proxies = result.moves.length;
  result.moves.forEach((m, i) => { if (!ticked.has(i)) { proxies--; add(m.receiver); } });
  unneededOf(result).forEach((name, k) => { if (!ticked.has(result.moves.length + k)) add(name); });
  if (proxies < 1) throw new Error('Tick at least one move to re-run around the rest');
  return { proxies, fixed };
}
