// Onboard mode (spec 3.4): every device the proxies hear, placed on a coarse grid, filtered by distance from the
// house, with an Add form. Candidates live only in the engine's memory; this page stores nothing but the slider.
import { filterCandidates, isNew, isRotating, whereText, hiddenText, stableOrder, assignNumbers, createPoller,
         identityText, riseText } from './onboard_logic.js';

const REFRESH_MS = 10000;          // the panel updates every 10 s (Nick 2026-09-25: 3 s reshuffled it under the cursor)

export function initOnboard(ctx) {
  const { THREE, CSS2DObject, scene, P, show, toast, esc, $ } = ctx;
  const S = { active: false, data: null, latest: null, hover: null, adding: null, paused: false,
              order: [], nums: {}, resort: true,
              showOut: localStorage.getItem('onbShowOut') === '1', showRot: localStorage.getItem('onbShowRot') === '1',
              sort: localStorage.getItem('onbSort') || 'new' };
  const frozen = () => S.paused || !!S.adding;          // paused by hand, or while the Add form is open
  const g = new THREE.Group(); g.visible = false; scene.add(g);
  const marks = {};

  async function api(method, path, body) {
    const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json' },
                                  body: body === undefined ? undefined : JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    return j;
  }
  const poller = createPoller({                          // polling also keeps the engine's lease alive when frozen
    fetch: () => api('GET', 'api/onboarding'),
    apply: (d) => { S.latest = d; if (!frozen() || !S.data) { S.data = S.latest; render(); } },
    isActive: () => S.active, schedule: (fn, ms) => setTimeout(fn, ms), cancel: (t) => clearTimeout(t), every: REFRESH_MS });
  function setActive(on) {
    S.active = on; g.visible = on; $('onbPanel').style.display = on ? '' : 'none';
    // the label renderer ignores the group's visibility: hide the number labels themselves (render() re-shows them)
    if (!on) Object.values(marks).forEach(m => { m.lbl.visible = false; });
    if (on) { render(); poller.start(); } else poller.stop();
  }

  const where = (c) => esc(whereText(c));
  function shown() {
    const all = S.data?.candidates || [];
    S.order = stableOrder(S.order, all, S.sort, S.resort); S.resort = false;
    assignNumbers(S.nums, S.order);
    const byKey = Object.fromEntries(all.map(c => [c.key, c]));
    return filterCandidates(S.order.map(k => byKey[k]), S.showOut, S.showRot);
  }

  function mark(c, n) {
    let m = marks[c.key];
    if (!m) {
      const ring = new THREE.Mesh(new THREE.TorusGeometry(0.22, 0.05, 8, 24), new THREE.MeshBasicMaterial({ color: 0xe5e7eb }));
      ring.rotation.x = Math.PI / 2;
      const d = document.createElement('div'); d.className = 'lbl onb';
      d.onmouseenter = () => { S.hover = c.key; highlight(); }; d.onmouseleave = () => { S.hover = null; highlight(); };
      const lbl = new CSS2DObject(d);
      g.add(ring); g.add(lbl);
      m = marks[c.key] = { ring, lbl, d };
    }
    m.d.textContent = n;
    const p = P(c.floor, c.x, c.y, 1.0);
    m.ring.position.copy(p); m.lbl.position.set(p.x, p.y + 0.35, p.z);
    m.ring.visible = m.lbl.visible = show.floors.has(c.floor);
    return m;
  }
  function highlight() {
    Object.entries(marks).forEach(([k, m]) => {
      const on = k === S.hover; m.ring.material.color.setHex(on ? 0xfacc15 : 0xe5e7eb); m.ring.scale.setScalar(on ? 1.6 : 1);
      m.d.classList.toggle('hl', on);
    });
    document.querySelectorAll('#onbList tr[data-k]').forEach(tr => tr.classList.toggle('hl', tr.dataset.k === S.hover));
  }

  function render() {
    const d = S.data || { status: 'building', candidates: [], one_proxy: 0 };
    const { vis, far, rotating } = shown();
    const onMap = vis.filter(c => !c.outside);            // an outside device's in-house position means nothing
    const live = new Set(onMap.map(c => c.key));
    Object.entries(marks).forEach(([k, m]) => { if (!live.has(k)) { g.remove(m.ring); g.remove(m.lbl); delete marks[k]; } });
    onMap.forEach(c => mark(c, S.nums[c.key]));
    let msg = '';
    if (d.status === 'building') msg = 'Preparing the map of nearby devices…';
    else if (d.status === 'waiting') msg = 'Waiting for the bridge to send nearby devices…';
    else if (d.status === 'error') msg = 'Onboarding is unavailable: ' + esc(d.error);
    else if (d.bridge_outdated) msg = 'Update the RTLS@Home integration to use onboarding.';
    else if (S.adding) msg = 'Paused while you add a device.';
    else if (S.paused) msg = 'Paused: the list is not updating.';
    const inp = $('onbName'), keep = inp && { v: inp.value, f: document.activeElement === inp, k: $('onbKind').value,
                                              a: inp.selectionStart, b: inp.selectionEnd };
    $('onbPanel').innerHTML =
      `<h3>Onboard a device</h3>` + (msg ? `<div class="muted">${msg}</div>` : '') +
      `<div class="ctl"><label><input type="checkbox" id="onbOut"${S.showOut ? ' checked' : ''}> ` +
      `Show devices that are probably outside</label></div>` +
      `<div class="ctl"><label><input type="checkbox" id="onbRot"${S.showRot ? ' checked' : ''}> ` +
      `Show phones, watches and laptops (their addresses rotate)</label></div>` +
      `<div class="muted">${hiddenText(far, d.one_proxy || 0, rotating)}</div>` + checkLine(d.check) +
      `<div class="muted">To find a device: switch it on or put its battery in (it shows as new), or hold it next ` +
      `to a proxy for 15 s and sort by rising.</div>` +
      `<div class="ctl">Sort: ${['new', 'rise', 'loud', 'name'].map(s => `<button data-sort="${s}" class="${S.sort === s ? 'on' : ''}">` +
        `${{ new: 'newest', rise: 'rising', loud: 'loudest', name: 'name' }[s]}</button>`).join('')}` +
      ` <button data-pause="1" class="${S.paused ? 'on' : ''}">${S.paused ? '▶ Resume' : '⏸ Pause'}</button>` +
      ` <button data-refresh="1" title="show the latest and re-sort">↻ Refresh</button></div>` +
      `<table id="onbList">${vis.map(c => row(c, S.nums[c.key])).join('')}</table>`;
    const again = $('onbName');                // re-rendered every poll: keep what is being typed
    if (keep && again) {
      again.value = keep.v; $('onbKind').value = keep.k;
      if (keep.f) { again.focus(); again.setSelectionRange(keep.a, keep.b); }
    }
    highlight();
  }
  function checkLine(chk) {
    if (!chk || !chk.n) return '';
    const tip = chk.rows.map(r => `${r.name}: tracker ${r.tracker}, locator ${r.locator}${r.outside ? ' (outside?)' : ''}`).join(' · ');
    return `<div class="muted" title="${esc(tip)}">Locator check on your tracked devices: same room ${chk.same_room}/${chk.n}, ` +
           `same floor ${chk.same_floor}/${chk.n}</div>`;
  }
  function row(c, n) {
    const up = riseText(c), ident = identityText(c);
    const tip = [...(c.why || []), c.adv_tx != null ? `advertised transmit power ${c.adv_tx} dBm` : ''].filter(Boolean).join(' · ');
    const badges = (isNew(c, Date.now() / 1000) ? '<span class="pill live">new</span> ' : '') +
                   (up ? `<span class="pill live" title="signal rose this much since the last update">${esc(up)}</span> ` : '') +
                   (c.settling ? '<span class="pill stale" title="under 20 s of readings: still settling">settling</span> ' : '') +
                   `<span class="muted">${c.ibeacon ? 'iBeacon' : 'MAC'}</span>`;
    const add = isRotating(c) ? '<span class="muted" title="phase 3">rotating address — needs an identity key</span>'
                            : `<button data-add="${esc(c.key)}">Add</button>`;
    let html = `<tr data-k="${esc(c.key)}"><td class="num">${n}</td><td>${esc(c.name || 'unnamed')} ${badges}` +
               (ident ? `<div title="${esc(tip)}">${esc(ident)}</div>` : '') +
               `<div class="muted">${esc(c.key)}</div></td>` +
               `<td>${where(c)}${c.nearest ? `<div class="muted">loudest at ${esc(c.nearest)}</div>` : ''}</td>` +
               `<td class="num muted">${c.rssi ?? ''} dB · ${c.proxies}</td><td>${add}</td></tr>`;
    if (S.adding === c.key) {
      html += `<tr class="addform"><td></td><td colspan="4"><input type="text" id="onbName" maxlength="64" placeholder="Friendly name" value="${esc(c.name || '')}">` +
              ` <select id="onbKind"><option value="tag">Tag</option><option value="phone">Phone</option></select>` +
              ` <button data-save="${esc(c.key)}">Add</button> <button data-cancel="1">Cancel</button></td></tr>`;
    }
    return html;
  }

  $('onbPanel').addEventListener('input', (ev) => {
    if (ev.target.id === 'onbOut') { S.showOut = ev.target.checked; localStorage.setItem('onbShowOut', S.showOut ? '1' : '0'); }
    else if (ev.target.id === 'onbRot') { S.showRot = ev.target.checked; localStorage.setItem('onbShowRot', S.showRot ? '1' : '0'); }
    else return;
    render();
  });
  $('onbPanel').addEventListener('mouseover', (ev) => {
    const tr = ev.target.closest('tr[data-k]'); const k = tr ? tr.dataset.k : null;
    if (k !== S.hover) { S.hover = k; highlight(); }
  });
  $('onbPanel').addEventListener('click', async (ev) => {
    const b = ev.target.closest('button'); if (!b) return;
    if (b.dataset.sort) { S.sort = b.dataset.sort; S.resort = true; localStorage.setItem('onbSort', S.sort); return render(); }
    if (b.dataset.pause) { S.paused = !S.paused; if (!frozen() && S.latest) S.data = S.latest; return render(); }
    if (b.dataset.refresh) { if (S.latest) S.data = S.latest; S.resort = true; return render(); }
    if (b.dataset.add) { S.adding = b.dataset.add; render(); $('onbName')?.focus(); return; }
    if (b.dataset.cancel) { S.adding = null; if (!frozen() && S.latest) S.data = S.latest; return render(); }
    if (b.dataset.save) {
      const name = $('onbName').value.trim(), kind = $('onbKind').value;
      if (!name) return toast('give it a name');
      try {
        await api('POST', 'api/devices', { key: b.dataset.save, name, kind, role: 'tracked' });
        toast('added: ' + name); S.adding = null;
        const gone = (d) => d && { ...d, candidates: d.candidates.filter(c => c.key !== b.dataset.save) };
        S.data = frozen() ? gone(S.data) : gone(S.latest || S.data); S.latest = gone(S.latest); render();
      } catch (e) { toast(e.message); }
    }
  });

  return { setActive };
}
