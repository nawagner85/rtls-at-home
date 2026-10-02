// The placement advisor in Setup -> Outlets (spec docs/specs/2026-10-01-placement-advisor-design.md): how many
// proxies and which receivers stay put; the engine searches the surveyed outlets in the background (progress, cancel);
// the result - today vs the suggestion, who goes where, what more proxies would add - is highlighted on the outlets,
// and "Use this layout" writes it into the map editor's draft (receiver moves and new proxies) to review and Apply.
// Rows can be unticked to take part of it (spec section 4): check that selection, use it, or re-run around it.
import { stepText, moveRows, suggestedOutlets, moreRows, pct, useLayout, selectionKeys, isPartial, selectedResult,
         changesIn, rerunRequest } from './advisor_logic.js';
import { isoLocal, escHtml as esc } from './map_logic.js';

export function initAdvisor({ $, toast, outlets, map, openMap, floorNames }) {
  const panel = $('advisorPanel');
  const S = { active: false, st: null, receivers: [], fixed: null, n: null, timer: null, busy: false, using: false,
              ticked: new Set(), tickedFor: undefined };       // the ticked rows of the result computed at tickedFor

  async function api(method, path, body) {
    const r = await fetch(path, { method, headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
                                  body: body === undefined ? undefined : JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(j.error || 'HTTP ' + r.status), { reply: j });
    return j;
  }
  const running = () => S.st?.state === 'running';
  const isEcho = (name) => /Show/.test(name);

  async function refresh() {
    try { S.st = await api('GET', 'api/placement'); } catch (e) { S.st = S.st || null; }
    if (S.fixed === null && S.receivers.length) {                // first sight: the last request, else the Shows stay
      S.fixed = new Set(S.st?.request ? S.st.request.fixed : S.receivers.filter(isEcho));
      S.n = S.st?.request ? S.st.request.proxies : null;
    }
    const res = S.st?.result;
    if (res && S.tickedFor !== S.st.computed) {                // a new suggestion: everything ticked
      S.ticked = new Set([...Array(res.moves.length + (res.unneeded || []).length).keys()]); S.tickedFor = S.st.computed;
    }
    showSuggested();
    render();
    clearTimeout(S.timer);
    if (S.active && running()) S.timer = setTimeout(refresh, 1500);
  }
  async function setActive(on) {
    S.active = on; panel.style.display = on ? '' : 'none';
    clearTimeout(S.timer);
    if (!on) return outlets.setSuggested([]);
    try { S.receivers = ((await api('GET', 'api/house/doc')).receivers || []).map((r) => r.name); } catch (e) { /* kept */ }
    await refresh();
  }

  const showSuggested = () => outlets.setSuggested(S.st?.result ? suggestedOutlets(selectedResult(S.st.result, S.ticked)) : []);
  const movable = () => S.receivers.filter((r) => !S.fixed?.has(r));
  const nProxies = () => S.n ?? movable().length;

  function render() {
    if (!S.active) return;
    const st = S.st || { state: 'idle' }, res = st.result;
    const rows = res ? moveRows(res, floorNames) : [];
    const more = res ? moreRows(res.curve, st.request.proxies) : [];
    const partial = res ? isPartial(res, S.ticked) : false;
    const keys = res ? selectionKeys(res, S.ticked) : [];
    const mine = partial && st.check && st.check.keys.join('|') === keys.join('|') ? st.check : null;   // this selection's
    panel.innerHTML = `
      <h3>Suggest a layout</h3>
      <div class="muted">Where should the proxies go? The advisor tries the outlets above (not the deaf ones) and where
        each proxy is now, and scores each layout by how well the live model would place phones in every room.</div>
      <div class="ctl">Proxies <input id="advN" type="number" min="1" max="30" value="${nProxies()}" ${running() ? 'disabled' : ''}>
        <span class="muted">today ${movable().length}; more than today adds new ones</span></div>
      <div class="muted">Stay where they are:</div>
      <div class="advFixed">${S.receivers.map((r) => `<label><input type="checkbox" data-fix="${esc(r)}"
        ${S.fixed?.has(r) ? 'checked' : ''} ${running() ? 'disabled' : ''}> ${esc(r)}</label>`).join('')}</div>
      <div class="ctl">${running() ? '<button id="advCancel">Cancel</button>'
        : `<button id="advRun" class="primary">${res ? 'Run again' : 'Suggest a layout'}</button>`}</div>
      ${running() ? `<div class="advProg">${st.asked?.check ? 'Checking your selection: ' : ''}${esc(stepText(st.progress))}</div>` : ''}
      ${st.state === 'failed' ? `<div class="bad">The advisor stopped: ${esc(st.error || 'unknown error')}</div>` : ''}
      ${st.state === 'cancelled' && !running() ? '<div class="muted">Cancelled.</div>' : ''}
      ${res ? `<div class="box">
        ${st.stale ? '<div class="warn">The house or the outlets changed since this was worked out: run it again for a current answer.</div>' : ''}
        <div><b>${st.request.proxies} proxies</b>${st.request.fixed.length ? ` with ${esc(st.request.fixed.join(', '))} where they are` : ''}</div>
        <table class="advScore"><tr><th></th><th>room</th><th>floor</th></tr>
          <tr><td>Today</td><td>${pct(res.today.room)}</td><td>${pct(res.today.floor)}</td></tr>
          <tr class="sug"><td>Suggested</td><td>${pct(res.suggested.room)}</td><td>${pct(res.suggested.floor)}</td></tr>
          ${mine ? `<tr class="mine"><td>Your selection</td><td>${pct(mine.room)}</td><td>${pct(mine.floor)}</td></tr>` : ''}</table>
        <table id="advMoves">${rows.map((r, i) => `<tr data-out="${esc(r.outlet ?? '')}" class="${r.what}${S.ticked.has(i) ? '' : ' off'}">
          <td><input type="checkbox" data-tick="${i}" ${S.ticked.has(i) ? 'checked' : ''} title="take this one"></td><td>${esc(r.who)}</td><td>${{ move: 'moves to', stays: 'stays', new: 'goes to', unplug: 'not needed' }[r.what]}</td>
          <td>${esc(r.where)}${r.outlet ? ` <span class="muted">${esc(r.outlet)}</span>` : ''}</td></tr>`).join('')}</table>
        ${more.length ? `<div class="muted">More proxies: ${more.map((m) => `${m.n}: room ${m.room} (${m.gain}), floor ${m.floor}`).join('; ')}</div>` : ''}
        <div class="muted">Simulated ${res.points} positions; chose from ${res.candidates} spots.</div>
        <div class="ctl"><button id="advUse" ${S.using ? 'disabled' : ''}>${partial ? 'Use the ticked moves' : 'Use this layout'}</button>
          <span class="muted">puts the moves into the map's draft to review and Apply</span></div>
        ${partial && !running() ? `<div class="ctl">${mine ? '' : '<button id="advCheck">Check this selection</button>'}
          <button id="advAround">Keep the unticked ones and re-run</button></div>
          <div class="muted">The suggestion was worked out as a whole: a part of it scores differently. A check takes about a
            minute; a re-run finds the best places for the rest, with the unticked ones where they are.</div>` : ''}
      </div>` : ''}`;
  }

  panel.addEventListener('change', (ev) => {
    const t = ev.target;
    if (t.dataset.fix != null) { t.checked ? S.fixed.add(t.dataset.fix) : S.fixed.delete(t.dataset.fix); render(); }
    if (t.id === 'advN') S.n = Math.max(1, Math.min(30, Math.round(+t.value || 1)));
    if (t.dataset.tick != null) { t.checked ? S.ticked.add(+t.dataset.tick) : S.ticked.delete(+t.dataset.tick); showSuggested(); render(); }
  });
  panel.addEventListener('click', async (ev) => {
    const b = ev.target.closest('button, tr[data-out]');
    if (!b || S.busy || ev.target.matches('input')) return;
    if (b.tagName === 'TR') { if (b.dataset.out) outlets.select(b.dataset.out); return; }
    S.busy = true;
    try {
      if (b.id === 'advRun') {
        S.st = await api('POST', 'api/placement', { proxies: nProxies(), fixed: [...(S.fixed || [])] }).catch((e) => {
          if (e.reply?.status) return e.reply.status;          // one was running already: show it
          throw e;
        });
        await refresh();
      } else if (b.id === 'advCancel') {
        S.st = await api('DELETE', 'api/placement'); await refresh();
      } else if (b.id === 'advCheck') {
        S.st = await api('POST', 'api/placement', { check: selectionKeys(S.st.result, S.ticked) }); await refresh();
      } else if (b.id === 'advAround') {
        const req = rerunRequest(S.st.request, S.st.result, S.ticked);
        S.fixed = new Set(req.fixed); S.n = req.proxies;
        S.st = await api('POST', 'api/placement', req); await refresh();
      } else if (b.id === 'advUse') {
        const chosen = selectedResult(S.st.result, S.ticked);
        if (!changesIn(chosen)) { toast('Nothing ticked changes the map'); return; }
        S.using = true; render();
        const out = await useLayout({ settle: () => map.settle(), getDraft: () => api('GET', 'api/house/draft'),
                                      getApplied: () => api('GET', 'api/house/doc'), putDraft: (d) => api('PUT', 'api/house/draft', d),
                                      reload: () => map.reload(), now: () => isoLocal() }, chosen);
        const parts = [out.moved.length && `${out.moved.length} moved`, out.added.length && `${out.added.length} new`]
          .filter(Boolean).join(', ');
        toast(`In the draft: ${parts || 'nothing to change'}${out.missing.length ? `; not in the draft: ${out.missing.join(', ')}` : ''}. Review it and Apply.`);
        openMap();
      }
    } catch (e) { toast(e.message); }
    finally { S.busy = false; S.using = false; render(); }
  });

  return { setActive };
}
