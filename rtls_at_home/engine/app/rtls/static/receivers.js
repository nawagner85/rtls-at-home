// Receivers page (Setup): turn specific proxies and Echo Shows on and off (Nick 2026-09-27). An off receiver is left out
// of tracking and onboarding the way a dead one is - its readings are ignored, not counted as silence - and its samples
// are still recorded. The server keeps the choice (sessions/receivers.json).
import { sortReceivers, statusText, corrText, summaryText } from './receivers_logic.js';
import { createPoller } from './onboard_logic.js';

const REFRESH_MS = 5000;

export function initReceivers({ $, esc, toast }) {
  const S = { active: false, rows: [], busy: false, error: null };

  async function api(method, body) {
    const r = await fetch('api/receivers', { method, headers: { 'Content-Type': 'application/json' },
                                             body: body === undefined ? undefined : JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    return j;
  }

  // one polling chain even if the page is opened twice while a request is in flight (the onboarding poller's rule)
  const poller = createPoller({
    fetch: async () => { try { return { rows: (await api('GET')).receivers || [] }; } catch (e) { return { error: e.message }; } },
    apply: (d) => { if (d.rows) S.rows = d.rows; S.error = d.error || null; render(); },
    isActive: () => S.active, schedule: (fn, ms) => setTimeout(fn, ms), cancel: (t) => clearTimeout(t), every: REFRESH_MS });

  function row(r) {
    const st = statusText(r), pill = st === 'live' ? 'live' : st === 'off' ? 'gray' : 'stale';
    const tip = [r.level != null ? `running level ${r.level > 0 ? '+' : ''}${r.level} dB` : '',
                 'the live anchor correction to its expected level'].filter(Boolean).join(' · ');
    return `<tr class="${r.enabled ? '' : 'rxoff'}">` +
           `<td><input type="checkbox" data-rx="${esc(r.name)}" aria-label="${esc(r.name)} on"` +
           `${r.enabled ? ' checked' : ''}${S.busy ? ' disabled' : ''}></td>` +
           `<td>${esc(r.name)}<div class="muted">${r.kind === 'show' ? 'Echo Show' : 'proxy'} · ${esc(r.room || '—')} · ` +
           `${esc(r.floor_name || '')}</div></td>` +
           `<td><span class="pill ${pill}">${esc(st)}</span></td>` +
           `<td class="num muted" title="${esc(tip)}">${esc(corrText(r))}</td></tr>`;
  }

  function render() {
    const rows = sortReceivers(S.rows);
    $('rxPanel').innerHTML = `<h3>Receivers</h3>` +
      `<div class="muted">Switch a receiver off to leave it out of tracking and onboarding. Its readings are ignored, ` +
      `not counted as silence, and they are still recorded.</div>` +
      (S.error ? `<div class="muted">Couldn't load the receivers: ${esc(S.error)}</div>` : '') +
      (rows.length ? `<div class="muted">${esc(summaryText(rows))}</div>` : '') +
      `<table id="rxList">${rows.map(row).join('')}</table>`;
  }

  $('rxPanel').addEventListener('change', async (ev) => {
    const name = ev.target.dataset && ev.target.dataset.rx;
    if (!name) return;
    const on = ev.target.checked;
    S.busy = true;
    try {
      S.rows = (await api('POST', { name, enabled: on })).receivers || S.rows;
      toast(`${name} switched ${on ? 'on' : 'off'}`);
    } catch (e) {
      toast(e.message);
    }
    S.busy = false;
    render();
  });

  function setActive(on) {
    S.active = on;
    $('rxPanel').style.display = on ? '' : 'none';
    if (on) poller.start(); else poller.stop();
  }

  return { setActive };
}
