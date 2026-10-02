// Outlets mode (outlet survey, Nick 2026-09-25): no tracking - the floors and furniture, and every outlet marked so
// far. Tap next to a wall to add an outlet there (standard 16 in or counter 44 in); select one to change its height,
// note it, nudge it along the wall (arrow keys, 5 cm; shift 25 cm) or delete it. Stored by the engine
// (api/outlets -> sessions/outlets.json); tools/fetch_outlets.py hands them to the proxy placement.
import { makeRaster, nearestXY, startFloor } from './calib_geom.js';
import { snapToWall, nudgeXY, statusKind, sortOutlets, heightText, createLatestSender, clip } from './outlets_logic.js';

const OFFSET = 0.03, RADIUS = 0.6;               // an outlet sits 3 cm off the wall face; taps within 0.6 m of one
const COLOR = { current: 0x22c55e, deaf: 0xef4444, previous: 0x94a3b8, candidate: 0xfacc15 };
const SUGGESTED = 0x22d3ee;                      // the placement advisor's outlets (advisor.js)

export function initOutlets(ctx) {
  const { THREE, CSS2DObject, scene, camera, renderer, house, roomMeshes, show, applyExplode, topView, P, toast, esc, $ } = ctx;
  let preset = 'standard';
  try { preset = localStorage.getItem('outHeight') || 'standard'; } catch (e) { /* private window */ }
  const S = { active: false, floor: 1, data: null, sel: null, confirm: false, sug: new Map() };   // sug: the advisor's outlets
  const g = new THREE.Group(); g.visible = false; scene.add(g);
  const rasters = {};
  const R = (fi) => (rasters[fi] ||= makeRaster(house, fi));

  async function api(method, path, body) {
    const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json' },
                                  body: body === undefined ? undefined : JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    return j;
  }
  async function load() { S.data = await api('GET', 'api/outlets'); render(); draw(); }
  const outlets = () => S.data?.outlets || [];
  const sel = () => outlets().find(o => o.id === S.sel) || null;

  function setFloor(fi) {
    S.floor = fi; show.floors = new Set([fi]); applyExplode(); ctx.floorBtnSync(fi); topView(); render(); draw();
  }
  async function setActive(on) {
    S.active = on; g.visible = on; $('outPanel').style.display = on ? '' : 'none';
    if (!on) {
      clear(); S.sel = null; S.confirm = false;
      show.floors = new Set(house.floors.map((_, i) => i)); applyExplode(); ctx.floorBtnSync(null); return;
    }
    setFloor(startFloor(S.floor, house.floors.length));   // a one-floor house has no floor 1
    try { await load(); } catch (e) { toast('outlets: ' + e.message); }
  }

  // ---------- panel ----------
  function render() {
    if (!S.active) return;
    const here = sortOutlets(outlets().filter(o => o.floor === S.floor));
    const unplaced = outlets().filter(o => o.x == null || o.y == null);
    const o = sel();
    const hBtn = (h, cur, attr) => `<button ${attr}="${h}" class="${cur === h ? 'on' : ''}">${h === 'standard' ? 'Standard 16"' : 'Counter 44"'}</button>`;
    $('outPanel').innerHTML = `
      <div class="ctl">${house.floors.map((f, i) => `<button data-ofl="${i}" class="${i === S.floor ? 'on' : ''}">${esc(f.name)}</button>`).join('')}</div>
      <div class="ctl">new outlets: ${hBtn('standard', preset, 'data-preset')}${hBtn('counter', preset, 'data-preset')}</div>
      <div class="muted">Tap next to a wall to add an outlet. Select one to edit; arrow keys nudge it (shift: 25 cm).</div>
      ${o ? `<div class="box" id="outSel">
        <b>${esc(o.id)}</b> · ${esc(o.room || '—')} · ${o.x == null ? '<i>not placed - tap the map</i>' : `${o.x.toFixed(2)}, ${o.y.toFixed(2)}`}
        <div class="ctl">${hBtn('standard', o.height, 'data-oh')}${hBtn('counter', o.height, 'data-oh')}
          ${o.height === 'measured' ? `<span class="muted">measured ${esc(heightText(o))}</span>` : ''}</div>
        <div class="ctl"><input id="outNote" maxlength="200" placeholder="note (behind sofa, switched, GFCI...)" value="${esc(o.note || '')}">
          <button id="outNoteSave">Save note</button></div>
        <div class="muted">${esc(o.status || '')}</div>
        <div class="ctl">${S.confirm ? `Delete ${esc(o.id)}? <button id="outDelYes">Delete</button> <button id="outDelNo">Keep</button>`
                                    : '<button id="outDel">Delete…</button>'} <button id="outDesel">Done</button></div>
      </div>` : ''}
      <table id="outList">${here.map(x => `<tr data-oid="${esc(x.id)}" class="${x.id === S.sel ? 'hl' : ''}">
        <td><span class="dot" style="background:#${COLOR[statusKind(x.status)].toString(16).padStart(6, '0')}"></span>${S.sug.has(x.id) ? '<span class="sugmark" title="suggested">◆</span>' : ''}</td>
        <td>${esc(x.room || '—')}</td><td>${esc(heightText(x))}</td><td class="muted" title="${esc(x.note || '')}">${esc(clip(x.note, 60))}</td></tr>`).join('')}</table>
      <div class="muted">${here.length} on this floor · ${outlets().length} in all${unplaced.length ? ` · ${unplaced.length} not placed: ` +
        unplaced.map(u => `<a href="#" data-oid="${esc(u.id)}">${esc(u.id)}</a>`).join(', ') : ''}</div>`;
  }

  $('outPanel').addEventListener('click', async (ev) => {
    const b = ev.target.closest('button, tr[data-oid], a[data-oid]');
    if (!b) return;
    if (b.tagName === 'A') ev.preventDefault();
    try {
      if (b.dataset.ofl != null) return setFloor(+b.dataset.ofl);
      if (b.dataset.preset) {
        preset = b.dataset.preset;
        try { localStorage.setItem('outHeight', preset); } catch (e) { /* private window */ }
        return render();
      }
      if (b.dataset.oid) return select(b.dataset.oid);
      const o = sel(); if (!o) return;
      if (b.dataset.oh) return await patch(o.id, { height: b.dataset.oh });
      if (b.id === 'outNoteSave') return await patch(o.id, { note: $('outNote').value });
      if (b.id === 'outDel') { S.confirm = true; return render(); }
      if (b.id === 'outDelNo') { S.confirm = false; return render(); }
      if (b.id === 'outDelYes') { await api('DELETE', 'api/outlets/' + encodeURIComponent(o.id)); S.sel = null; S.confirm = false; return await load(); }
      if (b.id === 'outDesel') { S.sel = null; S.confirm = false; render(); return draw(); }
    } catch (e) { toast(e.message); }
  });

  function select(id) {
    S.sel = id; S.confirm = false;
    const o = sel(); if (o && o.floor != null && o.floor !== S.floor && o.x != null) return setFloor(o.floor);
    render(); draw();
  }
  // The placement advisor's suggestion: [{id, label}] - a ring and the proxy's name on each of those outlets.
  function setSuggested(list) {
    S.sug = new Map((list || []).map(x => [x.id, x.label])); render(); draw();
  }

  async function patch(id, body) {
    const o = await api('PATCH', 'api/outlets/' + encodeURIComponent(id), body);
    const i = S.data.outlets.findIndex(x => x.id === id); if (i >= 0) S.data.outlets[i] = o;
    render(); draw(); return o;
  }

  // ---------- tap to add, or to place the selected unplaced outlet ----------
  const ray = new THREE.Raycaster(); let down = null;
  renderer.domElement.addEventListener('pointerdown', (e) => { if (S.active) down = { x: e.clientX, y: e.clientY, t: performance.now() }; });
  renderer.domElement.addEventListener('pointerup', async (e) => {
    if (!S.active || !down) return;
    if (Math.hypot(e.clientX - down.x, e.clientY - down.y) > 8 || performance.now() - down.t > 600) return;
    const r = renderer.domElement.getBoundingClientRect();
    ray.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), camera);
    const hit = ray.intersectObjects(roomMeshes.filter(m => m.userData.floor === S.floor), false)[0];
    if (!hit) return toast('tap inside the house, next to a wall');
    const s = snapToWall(nearestXY(R(S.floor), hit.point.x, -hit.point.z), hit.point.x, -hit.point.z, OFFSET, RADIUS);
    if (!s) return toast('tap next to a wall - outlets are on walls');
    try {
      const o = sel();
      if (o && (o.x == null || o.y == null)) await patch(o.id, { floor: S.floor, x: s.x, y: s.y });
      else {
        const n = await api('POST', 'api/outlets', { floor: S.floor, x: s.x, y: s.y, height: preset });
        S.data.outlets.push(n); S.sel = n.id; S.confirm = false; render(); draw();
      }
    } catch (err) { toast(err.message); }
  });

  // arrow keys nudge the selected outlet (it re-snaps to the nearest wall); the marker moves at once and one save
  // goes out when the keys stop (createLatestSender)
  const nudges = createLatestSender({ send: (id, body) => patch(id, body).catch(err => { toast(err.message); load(); }),
                                      schedule: (fn, ms) => setTimeout(fn, ms), cancel: (t) => clearTimeout(t), delay: 400 });
  window.addEventListener('keydown', (e) => {
    const o = sel();
    if (!S.active || !o || o.x == null || e.target.closest?.('input, textarea, select')) return;
    const n = nudgeXY(o.x, o.y, e.key, e.shiftKey); if (!n) return;
    e.preventDefault();
    const s = snapToWall(nearestXY(R(o.floor), n.x, n.y), n.x, n.y, OFFSET, RADIUS) || n;
    o.x = s.x; o.y = s.y; draw();
    nudges.push(o.id, { x: s.x, y: s.y });
  });

  // ---------- 3-D markers ----------
  function clear() {
    for (const c of [...g.children]) { g.remove(c); if (c.element) c.element.remove(); c.geometry?.dispose(); c.material?.dispose(); }
  }
  function draw() {
    clear();
    if (!S.active) return;
    for (const o of outlets()) {
      if (o.x == null || o.y == null || o.floor !== S.floor) continue;
      const col = COLOR[statusKind(o.status)];
      const box = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.14, 0.1), new THREE.MeshBasicMaterial({ color: col }));
      box.position.copy(P(o.floor, o.x, o.y, o.z_rel)); g.add(box);
      if (o.id === S.sel) {
        const ring = new THREE.Mesh(new THREE.TorusGeometry(0.22, 0.03, 8, 32), new THREE.MeshBasicMaterial({ color: 0xffffff }));
        ring.rotation.x = Math.PI / 2; ring.position.copy(P(o.floor, o.x, o.y, 0.03)); g.add(ring);
      }
      if (S.sug.has(o.id)) {
        const ring = new THREE.Mesh(new THREE.TorusGeometry(0.34, 0.04, 8, 40), new THREE.MeshBasicMaterial({ color: SUGGESTED }));
        ring.rotation.x = Math.PI / 2; ring.position.copy(P(o.floor, o.x, o.y, 0.05)); g.add(ring);
        const t = document.createElement('div'); t.className = 'lbl sug'; t.textContent = S.sug.get(o.id);
        const l = new CSS2DObject(t); l.position.copy(P(o.floor, o.x, o.y, o.z_rel + 0.6)); g.add(l);
      }
      const d = document.createElement('div'); d.className = 'lbl out' + (o.id === S.sel ? ' hl' : '');
      d.textContent = o.height === 'counter' ? 'C' : o.height === 'measured' ? 'M' : 'S';
      const l = new CSS2DObject(d); l.position.copy(P(o.floor, o.x, o.y, o.z_rel + 0.25)); g.add(l);
    }
  }
  return { setActive, setSuggested, select };
}
