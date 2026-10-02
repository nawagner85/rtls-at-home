// Calibrate tab (spec: docs/specs/2026-09-24-calibration-tab-design.md).
// Place poles or loose tags on the map (tap, snap, nudge), capture on the server, review, keep.
import { makeRaster, nearestXY, snapPoint, nudge, fmtLen, STEP, startFloor, tagName } from './calib_geom.js';

const IN = 0.0254;
const PRESETS = [['floor', 0], ['bed 24 in', 24 * IN], ['table 30 in', 30 * IN], ['counter 36 in', 36 * IN]];
const POLE_COLOR = { A: 0x22d3ee, B: 0xa3e635 };
const LOOSE_COLOR = 0xfbbf24;
const CONTEXTS = [['free', 'free'], ['on_surface', 'on a surface'], ['inside', 'inside something']];

export function initCalib(ctx) {
  const { THREE, CSS2DObject, scene, camera, renderer, house, roomMeshes, floorGroups, show, applyExplode, topView, P, toast, esc, $ } = ctx;
  const S = { active: false, rig: null, tags: {}, warnings: [], locked: false, type: 'pole', floor: 1, step: 'in',
              poles: { A: null, B: null }, loose: {}, sel: null, validation: false, co_location: false,
              countdown: 30, duration: 180, job: localStorage.getItem('calJob'), status: null, conv: null, cov: null };
  const rasters = {};
  const R = (fi) => (rasters[fi] ||= makeRaster(house, fi));
  const g = new THREE.Group(), kept = new THREE.Group();
  scene.add(g); scene.add(kept);
  let timers = [];

  // ---------- server ----------
  async function api(method, path, body) {
    const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json' },
                                  body: body === undefined ? undefined : JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    return j;
  }
  const units = () => S.rig?.units || 'in';
  const len = (m) => fmtLen(m, units());
  const toUser = (m) => units() === 'cm' ? +(m * 100).toFixed(1) : +(m / IN).toFixed(1);
  const fromUser = (v) => units() === 'cm' ? v / 100 : v * IN;
  const tagLabel = (k) => tagName(S.tags[k], k);
  const rigTags = () => (S.rig?.poles || []).flatMap(p => p.slots.map(s => s.tag_key)).filter(Boolean);

  async function loadRig() {
    const r = await api('GET', 'api/calib/rig');
    S.rig = r.rig; S.tags = r.tags; S.warnings = r.warnings; S.locked = r.locked;
    for (const k of rigTags()) S.loose[k] ||= { pos: null, height: 30 * IN, preset: 2, context: 'on_surface', note: '' };
  }

  // ---------- panel skeleton ----------
  $('calPanel').innerHTML = `
    <h3>Round</h3><div id="calRound"></div>
    <h3>Placement</h3><div id="calPlace"></div>
    <div class="ctl"><button id="calCapture" class="primary">Capture</button></div>
    <div id="calStatus" class="calcard muted"></div>
    <h3>Convergence</h3><div id="calConv" class="calcard muted">no check yet</div>
    <details><summary class="muted">Rig</summary><div id="calRig"></div></details>
    <details><summary class="muted">Counts (information only)</summary><div id="calCounts"></div></details>`;

  // ---------- round settings ----------
  function renderRound() {
    const fl = house.floors.map((f, i) => `<button data-fl="${i}" class="${i === S.floor ? 'on' : ''}">${esc(f.name)}</button>`).join('');
    $('calRound').innerHTML = `
      <div class="ctl">${fl}</div>
      <div class="ctl"><button data-ty="pole" class="${S.type === 'pole' ? 'on' : ''}">Pole round</button>
        <button data-ty="loose" class="${S.type === 'loose' ? 'on' : ''}">Loose round</button></div>
      <div class="ctl"><label><input type="checkbox" id="calVal" ${S.validation ? 'checked' : ''}> validation (never trained on)</label>
        ${S.type === 'loose' ? `<label><input type="checkbox" id="calCol" ${S.co_location ? 'checked' : ''}> co-location</label>` : ''}</div>
      <div class="ctl"><label>walk-away <input type="number" id="calCd" min="0" max="120" value="${S.countdown}"> s</label>
        <label>record <input type="number" id="calDur" min="60" max="600" value="${S.duration}"> s</label></div>`;
    $('calRound').querySelectorAll('[data-fl]').forEach(b => b.onclick = () => setFloor(+b.dataset.fl));
    $('calRound').querySelectorAll('[data-ty]').forEach(b => b.onclick = () => { S.type = b.dataset.ty; S.sel = null; renderRound(); renderPlace(); draw(); });
    $('calVal').onchange = (e) => S.validation = e.target.checked;
    if ($('calCol')) $('calCol').onchange = (e) => S.co_location = e.target.checked;
    $('calCd').onchange = (e) => S.countdown = +e.target.value;
    $('calDur').onchange = (e) => S.duration = +e.target.value;
  }
  function setFloor(fi) {
    S.floor = fi; show.floors = new Set([fi]); applyExplode(); ctx.floorBtnSync(fi); topView();
    renderRound(); draw();
  }

  // ---------- placement ----------
  const item = (key) => S.type === 'pole' ? S.poles[key] : S.loose[key]?.pos;
  function setItem(key, pos) { if (S.type === 'pole') S.poles[key] = pos; else S.loose[key].pos = pos; }
  function where(pos) {
    if (!pos) return '<span class="muted">not placed</span>';
    const room = roomAt(pos);
    return `${esc(room ?? 'outside every room')} <span class="muted">(${pos.x.toFixed(2)}, ${pos.y.toFixed(2)})${pos.floor !== S.floor ? ' · ' + esc(house.floors[pos.floor].name) : ''}</span>`;
  }
  function roomAt(pos) {
    const m = roomMeshes.find(r => r.userData.floor === pos.floor && inside(pos.x, pos.y, r.userData.points));
    return m ? m.userData.room : null;
  }
  function inside(x, y, pts) {
    if (!pts) return false;
    let c = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const [xi, yi] = pts[i], [xj, yj] = pts[j];
      if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) c = !c;
    }
    return c;
  }
  function renderPlace() {
    let h = '';
    if (S.type === 'pole') {
      for (const pole of S.rig?.poles || []) {
        const slots = pole.slots.map(s => `${s.slot} ${s.tag_key ? esc(tagLabel(s.tag_key)) : '—'} ${len(s.height_m)}`).join(' · ');
        h += `<div class="ctl"><button data-sel="${pole.id}" class="${S.sel === pole.id ? 'sel' : ''}">Pole ${pole.id}</button>
              ${where(S.poles[pole.id])} <button data-clr="${pole.id}">✕</button></div><div class="muted" style="margin:-2px 0 6px">${slots}</div>`;
      }
    } else {
      for (const k of rigTags()) {
        const L = S.loose[k];
        const opts = PRESETS.map(([n], i) => `<option value="${i}" ${L.preset === i ? 'selected' : ''}>${n}</option>`).join('') +
                     `<option value="-1" ${L.preset === -1 ? 'selected' : ''}>custom</option>`;
        const ctxs = CONTEXTS.map(([v, n]) => `<option value="${v}" ${L.context === v ? 'selected' : ''}>${n}</option>`).join('');
        h += `<div class="ctl"><button data-sel="${esc(k)}" class="${S.sel === k ? 'sel' : ''}">${esc(tagLabel(k))}</button>
              ${where(L.pos)} <button data-clr="${esc(k)}">✕</button></div>
              <div class="ctl" style="margin-top:-2px"><select data-pre="${esc(k)}">${opts}</select>
              ${L.preset === -1 ? `<input type="number" data-h="${esc(k)}" value="${toUser(L.height)}"> ${units()}` : ''}
              <select data-ctx="${esc(k)}">${ctxs}</select><input type="text" class="note" data-note="${esc(k)}" placeholder="note" value="${esc(L.note)}"></div>`;
      }
      if (!rigTags().length) h = '<div class="muted">Assign tags in the rig first.</div>';
    }
    const sel = S.sel && item(S.sel);
    let dims = '';
    if (sel) {
      const n = nearestXY(R(sel.floor), sel.x, sel.y);
      dims = `<div class="muted">nearest walls: ${n.x ? `${len(n.x.d)} ${n.x.side}` : '—'} · ${n.y ? `${len(n.y.d)} ${n.y.side}` : '—'}${sel.snapped?.x != null || sel.snapped?.y != null ? ' · snapped' : ''}</div>`;
    }
    h += `<div class="ctl">${S.sel ? `tap the map to place <b>${esc(S.type === 'pole' ? 'Pole ' + S.sel : tagLabel(S.sel))}</b>` : '<span class="muted">select a pole or tag, then tap the map</span>'}</div>
      <div class="ctl"><div class="nudge"><span></span><button data-n="N">▲</button><span></span><button data-n="W">◀</button><span></span><button data-n="E">▶</button><span></span><button data-n="S">▼</button><span></span></div>
        <div><button data-st="in" class="${S.step === 'in' ? 'on' : ''}">1 in</button> <button data-st="cm" class="${S.step === 'cm' ? 'on' : ''}">10 cm</button></div></div>${dims}`;
    $('calPlace').innerHTML = h;
    const q = (sel) => $('calPlace').querySelectorAll(sel);
    q('[data-sel]').forEach(b => b.onclick = () => { S.sel = b.dataset.sel; renderPlace(); draw(); });
    q('[data-clr]').forEach(b => b.onclick = () => { setItem(b.dataset.clr, null); renderPlace(); draw(); });
    q('[data-n]').forEach(b => b.onclick = () => {
      const p = S.sel && item(S.sel); if (!p) return;
      const np = nudge(p, b.dataset.n, STEP[S.step]), axis = 'EW'.includes(b.dataset.n) ? 'x' : 'y';
      setItem(S.sel, { ...p, ...np, snapped: { ...(p.snapped || {}), [axis]: null } }); renderPlace(); draw(); });
    q('[data-st]').forEach(b => b.onclick = () => { S.step = b.dataset.st; renderPlace(); });
    q('[data-pre]').forEach(el => el.onchange = () => { const L = S.loose[el.dataset.pre]; L.preset = +el.value;
      if (L.preset >= 0) L.height = PRESETS[L.preset][1]; renderPlace(); draw(); });
    q('[data-h]').forEach(el => el.onchange = () => { S.loose[el.dataset.h].height = fromUser(+el.value); draw(); });
    q('[data-ctx]').forEach(el => el.onchange = () => S.loose[el.dataset.ctx].context = el.value);
    q('[data-note]').forEach(el => el.oninput = () => S.loose[el.dataset.note].note = el.value);
  }

  // tap to place (only while the Calibrate tab is active)
  const ray = new THREE.Raycaster(); let down = null;
  renderer.domElement.addEventListener('pointerdown', (e) => { if (S.active) down = { x: e.clientX, y: e.clientY, t: performance.now() }; });
  renderer.domElement.addEventListener('pointerup', (e) => {
    if (!S.active || !down) return;
    if (Math.hypot(e.clientX - down.x, e.clientY - down.y) > 8 || performance.now() - down.t > 600) return;
    if (!S.sel) {                                    // nothing selected: take the first unplaced item
      const keys = S.type === 'pole' ? (S.rig?.poles || []).map(p => p.id) : rigTags();
      S.sel = keys.find(k => !item(k)) || null;
      if (!S.sel) return toast('select a pole or tag first');
    }
    const r = renderer.domElement.getBoundingClientRect();
    ray.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), camera);
    const hit = ray.intersectObjects(roomMeshes.filter(m => m.userData.floor === S.floor), false)[0];
    if (!hit) return toast('tap on the floor');
    const x = hit.point.x, y = -hit.point.z;
    const sp = snapPoint(R(S.floor), x, y, S.rig.snap_offset_m, S.rig.snap_radius_m ?? 0.6);
    setItem(S.sel, { floor: S.floor, x: sp.x, y: sp.y, snapped: sp.snapped });
    renderPlace(); draw();
  });

  // ---------- 3-D markers ----------
  function lbl(text, pos) {
    const d = document.createElement('div'); d.className = 'lbl'; d.textContent = text;
    const o = new CSS2DObject(d); o.position.copy(pos); return o;
  }
  function clearGroup(grp) {
    for (const c of [...grp.children]) {
      grp.remove(c);
      if (c.element) c.element.remove();
      c.geometry?.dispose(); c.material?.dispose();
    }
  }
  function draw() {
    clearGroup(g);
    if (!S.active) return;
    const mat = (c, o = 1) => new THREE.MeshBasicMaterial({ color: c, transparent: o < 1, opacity: o });
    const add = (m, pos) => { m.position.copy(pos); g.add(m); return m; };
    const keys = S.type === 'pole' ? (S.rig?.poles || []).map(p => p.id) : rigTags();
    for (const k of keys) {
      const p = item(k); if (!p || p.floor !== S.floor) continue;
      if (S.type === 'pole') {
        const pole = S.rig.poles.find(q => q.id === k), top = Math.max(...pole.slots.map(s => s.height_m)) + 0.1;
        add(new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.03, top, 8), mat(POLE_COLOR[k] ?? 0xffffff)), P(p.floor, p.x, p.y, top / 2));
        for (const s of pole.slots) if (s.tag_key)
          add(new THREE.Mesh(new THREE.SphereGeometry(0.08, 12, 8), mat(POLE_COLOR[k] ?? 0xffffff)), P(p.floor, p.x, p.y, s.height_m));
        g.add(lbl('Pole ' + k, P(p.floor, p.x, p.y, top + 0.15)));
      } else {
        const h = S.loose[k].height;
        add(new THREE.Mesh(new THREE.BoxGeometry(0.12, 0.12, 0.12), mat(LOOSE_COLOR)), P(p.floor, p.x, p.y, h + 0.06));
        g.add(lbl(tagLabel(k), P(p.floor, p.x, p.y, h + 0.3)));
      }
      if (k === S.sel) {
        const ring = add(new THREE.Mesh(new THREE.TorusGeometry(0.25, 0.03, 8, 32), mat(0xffffff)), P(p.floor, p.x, p.y, 0.03));
        ring.rotation.x = Math.PI / 2;
        const n = nearestXY(R(p.floor), p.x, p.y);           // dimension lines to the nearest walls
        for (const [ax, w] of [['x', n.x], ['y', n.y]]) {
          if (!w) continue;
          const end = ax === 'x' ? { x: w.face, y: p.y } : { x: p.x, y: w.face };
          const a = P(p.floor, p.x, p.y, 0.05), b = P(p.floor, end.x, end.y, 0.05);
          g.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([a, b]), new THREE.LineBasicMaterial({ color: 0xffffff })));
          const mid = ax === 'x' ? { x: (p.x + end.x) / 2, y: p.y + 0.18 } : { x: p.x + 0.18, y: (p.y + end.y) / 2 };
          g.add(lbl(len(w.d), P(p.floor, mid.x, mid.y, 0.05)));        // offset so the two labels never overlap
        }
      }
    }
  }
  function drawKept() {
    clearGroup(kept);
    if (!S.active || !S.cov) return;
    for (const p of S.cov.placements) {
      if (p.floor !== S.floor) continue;
      const m = new THREE.Mesh(new THREE.CircleGeometry(p.flagged ? 0.16 : 0.1, 16),
        new THREE.MeshBasicMaterial({ color: p.flagged ? 0xef4444 : (p.validation ? 0xa855f7 : 0x94a3b8), transparent: true, opacity: p.flagged ? 0.9 : 0.4, depthWrite: false }));
      m.rotation.x = -Math.PI / 2; m.position.copy(P(p.floor, p.x, p.y, 0.02)); kept.add(m);
    }
  }

  // ---------- capture ----------
  function body() {
    const placements = [];
    if (S.type === 'pole') {
      for (const pole of S.rig.poles) {
        const p = S.poles[pole.id]; if (!p) continue;
        for (const s of pole.slots) if (s.tag_key)
          placements.push({ tag_key: s.tag_key, pole: pole.id, slot: s.slot, floor: p.floor, x: p.x, y: p.y, height_m: s.height_m, context: 'free', snapped: p.snapped });
      }
    } else {
      for (const k of rigTags()) {
        const L = S.loose[k]; if (!L.pos) continue;
        placements.push({ tag_key: k, floor: L.pos.floor, x: L.pos.x, y: L.pos.y, height_m: L.height, context: L.context, note: L.note, snapped: L.pos.snapped });
      }
    }
    return { type: S.type, validation: S.validation, co_location: S.type === 'loose' && S.co_location,
             countdown_s: S.countdown, duration_s: S.duration, placements };
  }
  $('calCapture').onclick = async () => {
    const b = body();
    if (!b.placements.length) return toast('place a pole or tag first');
    try {
      const st = await api('POST', 'api/calib/capture', b);
      S.job = st.job_id; localStorage.setItem('calJob', S.job);
      (st.warnings || []).forEach(w => toast(w));
      pollStatus();
    } catch (e) { toast(e.message); }
  };
  async function act(path) {
    try {
      const r = await api('POST', path, {});
      if (path.endsWith('keep')) {
        toast('kept round ' + r.id);
        S.poles = { A: null, B: null }; for (const k in S.loose) { S.loose[k].pos = null; S.loose[k].note = ''; } S.sel = null;
        renderPlace(); draw(); pollCoverage(); pollConv();
      }
      pollStatus();
    } catch (e) { toast(e.message); }
  }

  function renderStatus() {
    const st = S.status; if (!st) return;
    let h = '';
    if (st.interrupted) h = '<b class="v-red">Capture interrupted (server restarted): redo it.</b>';
    else if (st.state === 'countdown') h = `<b>Walk away…</b> recording starts in ${Math.ceil(st.seconds_left)} s <button id="calCancel">cancel</button>`;
    else if (st.state === 'recording') {
      h = `<b>Recording…</b> ${Math.ceil(st.seconds_left)} s left <button id="calCancel">cancel</button><br>` +
          Object.entries(st.counts || {}).map(([t, n]) => `${esc(t)} ${n}`).join(' · ');
    } else if (st.state === 'review') {
      if (st.error) h = `<b class="v-red">Capture failed:</b> ${esc(st.error)}<br><button id="calRedo">Redo</button>`;
      else {
        const q = st.quality;
        h = '<table><tr><th>tag</th><th>verdict</th><th class="num">scanners</th><th class="num">samples</th></tr>' +
            Object.entries(q.tags).map(([t, v]) => `<tr><td>${esc(t)}</td><td class="v-${v.verdict}">${v.verdict}</td><td class="num">${v.scanners}/${q.receivers ?? 9}</td><td class="num">${v.samples}</td></tr>`).join('') + '</table>' +
            (q.dead.length ? `<div class="v-amber">dead during capture: ${q.dead.map(esc).join(', ')}</div>` : '') +
            (q.ha_gap_amber ? `<div class="v-amber">HA dropped out for ${q.ha_gaps_s} s</div>` : '') +
            (st.warnings || []).map(w => `<div class="v-amber">${esc(w)}</div>`).join('') +
            '<div class="ctl"><button id="calKeep" class="primary">Keep</button><button id="calRedo">Redo</button></div>';
      }
    } else {
      const lo = st.last_outcome;
      h = lo ? `last: ${esc(lo.outcome)}${lo.round_id ? ' ' + esc(lo.round_id) : ''}` : 'ready';
    }
    $('calStatus').innerHTML = h;
    $('calCapture').disabled = st.locked;
    if ($('calCancel')) $('calCancel').onclick = () => act('api/calib/cancel');
    if ($('calKeep')) $('calKeep').onclick = () => act('api/calib/keep');
    if ($('calRedo')) $('calRedo').onclick = () => act('api/calib/redo');
  }
  async function pollStatus() {
    try {
      const st = await api('GET', 'api/calib/status' + (S.job ? '?job=' + encodeURIComponent(S.job) : ''));
      if (S.job && (st.interrupted || (st.state === 'idle' && st.last_outcome?.job_id === S.job))) {
        S.job = null; localStorage.removeItem('calJob');
      }
      S.status = st; renderStatus();
    } catch (e) { $('calStatus').textContent = 'server unreachable'; }
  }

  // ---------- convergence and counts ----------
  function renderConv() {
    const c = S.conv; if (!c) return;
    const L = c.latest;
    let h = `<div class="muted">${c.running ? 'checking ' + esc(c.running) + ' · ' : ''}${c.queued} queued</div>`;
    if (!L) h += 'no check yet';
    else if (L.status === 'not_enough') h += `not enough rounds yet (${L.n_rounds})`;
    else if (L.status === 'failed') h += `<span class="v-red">check failed:</span> ${esc(L.error || '')}`;
    else {
      const sat = L.saturation?.saturated, uns = (L.stability?.constants || []).filter(x => x.unstable);
      h += `<b class="${sat && !uns.length ? 'v-green' : 'v-amber'}">${sat && !uns.length ? 'saturated: more data of this kind won’t help' : 'keep collecting'}</b>`;
      if (L.headline?.mid_median_err != null) h += `<br>pocket-height error ${L.headline.mid_median_err} m · all ${L.headline.all_median_err} m · room ${L.headline.room_acc}% · floor ${L.headline.floor_acc}%`;
      if (uns.length) h += '<br>unstable: ' + uns.slice(0, 4).map(x => `${esc(x.name)} (${esc(x.hint)})`).join('; ');
      if (L.rooms?.length) h += '<br>worst rooms: ' + L.rooms.slice(0, 3).map(r => `${esc(r.room ?? '?')} ${r.median_err} m`).join(', ');
      if (L.surprises?.length) h += '<br><span class="v-red">check these placements:</span> ' + L.surprises.map(s => `${esc(s.tag)} (${esc(s.reason)})`).join('; ');
    }
    $('calConv').innerHTML = h;
  }
  async function pollConv() { try { S.conv = await api('GET', 'api/calib/convergence'); renderConv(); } catch (e) { /* keep last */ } }
  function renderCounts() {
    const c = S.cov; if (!c) return;
    let h = '<table><tr><th>room</th><th class="num">pole</th><th class="num">loose</th><th class="num">val</th></tr>' +
      c.rooms.map(r => `<tr><td>${esc(house.floors[r.floor]?.name)} · ${esc(r.room ?? 'outside')}</td><td class="num">${r.pole_rounds}</td><td class="num">${r.loose_points}</td><td class="num">${r.validation_points}</td></tr>`).join('') + '</table>';
    h += '<table><tr><th>scanner</th><th class="num">&lt;3 m</th><th class="num">3–8 m</th><th class="num">&gt;8 m</th></tr>' +
      Object.entries(c.scanners).map(([s, b]) => `<tr><td>${esc(s)}</td>` + ['<3m', '3-8m', '>8m'].map(k => `<td class="num">${b[k].same}/${b[k].other}</td>`).join('') + '</tr>').join('') +
      '</table><div class="muted">links: same floor / other floor</div>';
    $('calCounts').innerHTML = h;
  }
  async function pollCoverage() { try { S.cov = await api('GET', 'api/calib/coverage'); renderCounts(); drawKept(); } catch (e) { /* keep last */ } }

  // ---------- rig editor ----------
  function renderRig() {
    const tagOpts = (cur) => '<option value="">—</option>' + Object.entries(S.tags).map(([k, t]) =>
      `<option value="${esc(k)}" ${k === cur ? 'selected' : ''}>${esc(tagName(t, k))}${t.configured ? '' : ' (not configured!)'}</option>`).join('');
    let h = '';
    for (const pole of S.rig.poles) {
      h += `<div class="ctl"><b>Pole ${pole.id}</b></div>` + pole.slots.map((s, i) =>
        `<div class="ctl">${s.slot} <select data-tag="${pole.id}|${i}">${tagOpts(s.tag_key)}</select>
         <input type="number" data-ht="${pole.id}|${i}" value="${toUser(s.height_m)}"> ${units()}</div>`).join('');
    }
    h += `<div class="ctl"><label>snap <input type="number" id="calSnap" value="${toUser(S.rig.snap_offset_m)}"> ${units()} from walls</label>
          <label>units <select id="calUnits"><option ${units() === 'in' ? 'selected' : ''}>in</option><option ${units() === 'cm' ? 'selected' : ''}>cm</option></select></label></div>
          <div class="ctl"><button id="calSaveRig" ${S.locked ? 'disabled' : ''}>Save rig</button></div>` +
         (S.warnings || []).map(w => `<div class="v-amber">${esc(tagLabel(w.tag_key))}: ${esc(w.warning)}</div>`).join('');
    $('calRig').innerHTML = h;
    const q = (sel) => $('calRig').querySelectorAll(sel);
    q('[data-tag]').forEach(el => el.onchange = () => { const [p, i] = el.dataset.tag.split('|'); S.rig.poles.find(x => x.id === p).slots[+i].tag_key = el.value || null; });
    q('[data-ht]').forEach(el => el.onchange = () => { const [p, i] = el.dataset.ht.split('|'); S.rig.poles.find(x => x.id === p).slots[+i].height_m = fromUser(+el.value); });
    $('calSnap').onchange = (e) => S.rig.snap_offset_m = fromUser(+e.target.value);
    $('calUnits').onchange = (e) => { S.rig.units = e.target.value; renderRig(); renderPlace(); draw(); };
    $('calSaveRig').onclick = async () => {
      try {
        const r = await api('PUT', 'api/calib/rig', { rig: S.rig });
        S.rig = r.rig; S.warnings = r.warnings; toast('rig saved');
        for (const k of rigTags()) S.loose[k] ||= { pos: null, height: 30 * IN, preset: 2, context: 'on_surface', note: '' };
        renderRig(); renderPlace(); draw();
      } catch (e) { toast(e.message); }
    };
  }

  // ---------- activation ----------
  async function setActive(on) {
    S.active = on;
    timers.forEach(clearInterval); timers = [];
    g.visible = kept.visible = on;
    if (!on) { clearGroup(g); clearGroup(kept); show.floors = new Set(house.floors.map((_, i) => i)); applyExplode(); ctx.floorBtnSync(null); return; }
    try { await loadRig(); } catch (e) { toast('rig: ' + e.message); return; }
    setFloor(startFloor(S.floor, house.floors.length)); renderRig(); renderPlace();
    pollStatus(); pollConv(); pollCoverage();
    timers.push(setInterval(pollStatus, 1000), setInterval(pollConv, 10000), setInterval(pollCoverage, 15000), setInterval(draw, 2000));
  }
  return { setActive };
}
