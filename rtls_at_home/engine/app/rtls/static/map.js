// The map editor (spec docs/specs/2026-09-30-map-builder-design.md, section 4): the Setup -> Map tab. Draws the draft
// house document a floor at a time as an SVG plan (metres, y up), selects and edits rooms, objects, doors and windows,
// open walls and stairs, saves the draft as you go (PUT api/house/draft) and lists its problems. Applying the draft
// to the engine is Phase C. URLs are relative (api/...), so the page also works behind Home Assistant's ingress.
import * as L from './map_logic.js';

const KIND_FILL = { room: '#3b82f6', closet: '#a78bfa', stairs: '#f59e0b', outdoor: '#22c55e' };
const MAT_COLOR = { light: '#d4d4d8', wood: '#d97706', stone: '#a8a29e', metal: '#94a3b8', glass: '#67e8f9', water: '#3b82f6',
                    drywall: '#e5e7eb', none: '#64748b' };
const TOOLS = [['select', 'V', 'Select'], ['room', 'R', 'Room'], ['door', 'D', 'Door'], ['window', 'W', 'Window'],
               ['garage_door', 'G', 'Garage'], ['opening', 'O', 'Doorway'], ['nowall', 'N', 'No wall'], ['object', 'B', 'Object'],
               ['wall', 'L', 'Wall'], ['stairs', 'S', 'Stairs'], ['measure', 'M', 'Measure'], ['image', 'I', 'Image'],
               ['receiver', 'P', 'Receiver']];
const ROOM_KINDS = { room: 'Room', closet: 'Closet', stairs: 'Stairs', outdoor: 'Outdoors' };
const AP_KINDS = { door: 'Door', window: 'Window', garage_door: 'Garage door', opening: 'Doorway (no door)' };
const AP_DEFAULTS = { window: [0.914, 2.032], door: [0, 2.032], garage_door: [0, 2.13], opening: [0, null] };
const MATERIALS = { light: 'Light (fabric, plastic)', wood: 'Wood', stone: 'Stone, tile, concrete', metal: 'Metal', glass: 'Glass',
                    water: 'Water', drywall: 'Drywall', none: 'Ignore (no effect on signal)' };
const CONSTRUCTIONS = { solid: 'Solid block', top: 'A top on legs', panel: 'A thin panel' };
const DOOR_MATERIALS = { wood: 'Wood', metal: 'Metal', glass: 'Glass' };
const DIRS = { '+x': 'right (→)', '-x': 'left (←)', '+y': 'up the page (↑)', '-y': 'down the page (↓)' };
const narrow = () => innerWidth <= 760;
const local = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private window */ } },
};

export function initMap({ $, toast, setup = false }) {
  const copy = L.applyCopy(setup);
  const esc = L.escHtml;                                           // attributes too: 12' 6" has both quotes
  const root = $('mapEditor'), panel = $('mapPanel');
  root.innerHTML = `
    <div id="mapTop">
      <span id="mapFloors" class="seg"></span>
      <button id="mapAbove" class="edit" title="Add a floor above this one">+ floor above</button>
      <button id="mapBelow" class="edit" title="Add a floor below this one">+ below</button>
      <span class="seg" id="mapUnits"><button data-u="ft-in">ft-in</button><button data-u="m">m</button></span>
      <button id="mapGrid" class="edit" title="Snap to a grid when no corner or wall is near">Grid</button>
      <button id="mapFit" title="Fit the floor in view (F)">Fit</button>
      <button id="mapUndo" class="edit" title="Undo (Ctrl+Z)">Undo</button><button id="mapRedo" class="edit" title="Redo (Ctrl+Shift+Z)">Redo</button>
      <button id="mapApply" class="primary edit">Apply</button>
      <button id="mapPrev" title="A 3-D view of the draft as the model will build it">3-D preview</button>
      <button id="mapProbs" title="Problems with the draft"></button>
      <span id="mapSave" class="muted" title="Click to save now"></span>
    </div>
    <div id="mapTools">${TOOLS.map(([t, k, n]) => `<button data-t="${t}" title="${n} (${k})">${k}<small>${n}</small></button>`).join('')}</div>
    <div id="mapStage"><svg id="mapSvg"></svg><div id="mapHint" class="muted"></div><div id="mapPreview" style="display:none"></div></div>`;
  const svg = $('mapSvg');
  const fileIn = Object.assign(document.createElement('input'), { type: 'file', accept: 'image/png,image/jpeg,image/webp' });
  fileIn.style.display = 'none';
  root.appendChild(fileIn);

  let doc = null, fi = 0, sel = null, tool = 'select', problems = [], hasDraft = false, active = false, loading = null;
  let hist = L.history(200), view = { cx: 0, cy: 0, s: 40 }, W = 0, H = 0, drag = null, spaceDown = false;
  let grid = local.get('mapGrid', '1') === '1';
  const saver = L.createSaver({
    send: () => api('PUT', 'api/house/draft', doc),
    onSaved: () => { hasDraft = true; renderTop(); loadProblems(); schedulePreview(); },
    onState: () => showStatus(),
    schedule: (fn, ms) => setTimeout(fn, ms), cancel: (t) => clearTimeout(t),
  });
  let places = { floors: [], areas: [] };                         // Home Assistant's floors and areas (the bridge)
  let preview = null, previewOn = local.get('mapPreview', '0') === '1', previewT = null, previewSeq = 0;

  let unitsView = local.get('mapUnits', '');                      // this browser's choice; else the house's
  const units = () => ((unitsView || doc?.display_units) === 'm' ? 'm' : 'ft-in');
  const fmt = (m) => L.fmtLength(m, units());
  const area = (a) => (units() === 'm' ? `${a.toFixed(1)} m²` : `${Math.round(a * 10.7639)} ft²`);
  const gridStep = () => (units() === 'm' ? 0.05 : 0.0508);              // 5 cm or 2 inches
  let applying = null, applyArmed = false, applyT = null, versions = [], lastApplied = null;   // Apply (spec section 5)
  const editable = () => !narrow() && !applying;
  const floor = () => doc.floors[fi];
  const X = (x) => (x - view.cx) * view.s + W / 2, Y = (y) => H / 2 - (y - view.cy) * view.s;
  const r1 = (v) => Math.round(v * 10) / 10;
  const toM = (e) => { const b = svg.getBoundingClientRect(); return [(e.clientX - b.left - W / 2) / view.s + view.cx, view.cy - (e.clientY - b.top - H / 2) / view.s]; };
  const poly = (pts) => pts.map((p) => `${r1(X(p[0]))},${r1(Y(p[1]))}`).join(' ');
  const line = (a, b, cls, extra = '') => `<line x1="${r1(X(a[0]))}" y1="${r1(Y(a[1]))}" x2="${r1(X(b[0]))}" y2="${r1(Y(b[1]))}" class="${cls}" ${extra}/>`;
  const text = (p, s, cls = '', dy = 0) => `<text x="${r1(X(p[0]))}" y="${r1(Y(p[1]) + dy)}" class="${cls}">${esc(s)}</text>`;
  const isSel = (kind, id) => !!sel && sel.fi === fi && sel.kind === kind && sel.id === id;
  const floorIndex = (id) => doc.floors.findIndex((f) => f.id === id);

  async function api(method, path, body) {
    const r = await fetch(path, { method, headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
                                  body: body === undefined ? undefined : JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status);
    return j;
  }

  // ---------- loading, editing, saving ----------
  async function loadPlaces() {
    try { places = await api('GET', 'api/ha/places'); } catch (e) { /* the editor works without them */ }
  }
  // The draft's problems, asked for after each save; a reply for an older request, or from before a reload, is dropped.
  let probSeq = 0;
  async function loadProblems() {
    const seq = ++probSeq;
    try {
      const r = await api('GET', 'api/house/draft/problems');
      if (seq !== probSeq) return;
      problems = r.problems || [];
      renderTop(); if (!sel) renderPanel();
    } catch (e) { /* the next save asks again */ }
  }
  async function load() {
    probSeq++;
    await saver.reset();                                             // a save still out lands first; its reply is dropped
    const [st] = await Promise.all([api('GET', 'api/house/draft'), loadPlaces()]);
    doc = L.ensureIds(st.doc); hasDraft = st.draft; problems = st.problems || [];
    hist = L.history(200); sel = null; drag = null;
    const want = floorIndex(local.get('mapFloor', ''));
    fi = want >= 0 ? want : doc.floors.reduce((b, f, i) => (f.rooms.length > doc.floors[b].rooms.length ? i : b), 0);
    fit(); everything();
    if (previewOn) showPreview(true);
    loadHistory();
    loadReceiverInfo();
    try {
      const st = await api('GET', 'api/house/apply');
      if (RUNNING.has(st.state)) { applying = st; everything(); pollApply(); }
      else if (st.state === 'done') { lastApplied = st.booted || st.finished; renderPanel(); }
    } catch (e) { /* no apply status yet */ }
  }
  function everything() { render(); renderTop(); renderPanel(); showStatus(); }

  // Every edit: the old document onto the undo stack, redraw, and a save 700 ms after the last edit.
  function commit(nd, prev = doc) {
    if (!nd || nd === prev) return;
    hist.push(prev); doc = L.withFrame(nd); changed();              // a room west or south of the origin moves it out
  }
  function changed() {
    if (sel && !L.itemOf(doc, sel)) sel = null;
    fi = Math.max(0, Math.min(fi, doc.floors.length - 1));
    saver.changed();
    everything();
  }
  function undo() { const d = hist.undo(doc); if (d) { doc = d; changed(); } }
  function redo() { const d = hist.redo(doc); if (d) { doc = d; changed(); } }
  function showStatus() {
    const el = $('mapSave'), st = saver.state;
    el.classList.toggle('bad', !!st.error);
    el.textContent = st.error ? `Not saved (${st.error}): trying again` : st.saving ? 'Saving...' : st.dirty ? 'Unsaved' :
      hasDraft ? 'Saved' : 'No changes';
  }
  $('mapSave').onclick = () => saver.flush();
  addEventListener('beforeunload', (e) => { if (saver.state.dirty) { saver.flush(); e.preventDefault(); } });

  // ---------- view ----------
  function measure() { const b = svg.getBoundingClientRect(); W = b.width; H = b.height; }
  function fit() {
    measure();
    let pts = floor().rooms.flatMap((r) => r.outline);
    if (!pts.length) pts = doc.floors.flatMap((f) => f.rooms.flatMap((r) => r.outline));
    if (!pts.length) { view = { cx: 5, cy: 5, s: 40 }; return; }
    const b = L.bbox(pts);
    view.cx = (b.x0 + b.x1) / 2; view.cy = (b.y0 + b.y1) / 2;
    view.s = Math.max(4, Math.min((W - 60) / Math.max(1, b.x1 - b.x0), (H - 60) / Math.max(1, b.y1 - b.y0)));
  }

  // ---------- drawing ----------
  const imgSize = {}, imgVer = {};
  const underlayUrl = (file) => 'api/house/underlay/' + file.split('/').pop() + (imgVer[file] ? '?v=' + imgVer[file] : '');
  function underlaySvg(u) {
    const url = underlayUrl(u.file), sz = imgSize[url];
    if (sz === undefined) {
      imgSize[url] = null;
      const im = new Image();
      im.onload = () => { imgSize[url] = [im.naturalWidth, im.naturalHeight]; render(); };
      im.onerror = () => { imgSize[url] = false; };
      im.src = url;
    }
    if (!sz) return '';
    const x = X(u.x), y = Y(u.y), w = sz[0] * u.m_per_px * view.s, h = sz[1] * u.m_per_px * view.s;
    return `<image href="${url}" x="${r1(x)}" y="${r1(y)}" width="${r1(w)}" height="${r1(h)}" opacity="${u.opacity ?? 0.5}"
      preserveAspectRatio="none" transform="rotate(${u.rotation || 0} ${r1(x)} ${r1(y)})"/>`;
  }
  function gridSvg() {
    let step = units() === 'm' ? 1 : 0.3048;
    while (step * view.s < 14) step *= 5;
    const x0 = view.cx - W / 2 / view.s, x1 = view.cx + W / 2 / view.s, y0 = view.cy - H / 2 / view.s, y1 = view.cy + H / 2 / view.s;
    let out = '';
    for (let x = Math.ceil(x0 / step) * step; x < x1; x += step) out += line([x, y0], [x, y1], 'grid');
    for (let y = Math.ceil(y0 / step) * step; y < y1; y += step) out += line([x0, y], [x1, y], 'grid');
    return out;
  }
  function apertureSvg(a) {
    const s = isSel('aperture', a.id) ? ' sel' : '', dx = a.b[0] - a.a[0], dy = a.b[1] - a.a[1], len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len, ny = dx / len, k = 3 / view.s;
    let out = line(a.a, a.b, 'cut');
    if (a.kind === 'door') {
      const tip = [a.a[0] + nx * len, a.a[1] + ny * len], rr = r1(len * view.s);
      out += line(a.a, tip, 'ap door' + s) +
        `<path d="M ${r1(X(tip[0]))} ${r1(Y(tip[1]))} A ${rr} ${rr} 0 0 1 ${r1(X(a.b[0]))} ${r1(Y(a.b[1]))}" class="ap arc${s}"/>`;
    } else if (a.kind === 'window') {
      out += [-1, 0, 1].map((j) => line([a.a[0] + nx * k * j, a.a[1] + ny * k * j], [a.b[0] + nx * k * j, a.b[1] + ny * k * j], 'ap window' + s)).join('');
    } else if (a.kind === 'garage_door') {
      out += line(a.a, a.b, 'ap garage' + s);
    } else {
      const t = 6 / view.s;
      out += [a.a, a.b].map((p) => line([p[0] - nx * t, p[1] - ny * t], [p[0] + nx * t, p[1] + ny * t], 'ap opening' + s)).join('');
    }
    return out;
  }
  function objectSvg(o) {
    const c = MAT_COLOR[o.material] || MAT_COLOR.none, s = isSel('object', o.id), b = L.bbox(o.outline);
    let out = `<polygon points="${poly(o.outline)}" fill="${c}" fill-opacity="${o.material === 'none' ? 0.04 : 0.22}"
      stroke="${s ? '#60a5fa' : c}" stroke-width="${s ? 2.5 : 1.2}"${o.construction === 'top' ? ' stroke-dasharray="4 3"' : ''}/>`;
    if ((b.x1 - b.x0) * view.s > 46 && (b.y1 - b.y0) * view.s > 14) out += text(L.centroid(o.outline), o.name || o.id, 'small', 4);
    return out;
  }
  function stairsSvg(st, f) {
    const lower = st.lower.floor === f.id;
    if (!lower && st.upper.floor !== f.id) return '';
    const room = f.rooms.find((r) => r.id === (lower ? st.lower.room : st.upper.room));
    if (!room || !st.foot || !st.top) return '';
    let out = '';
    if (lower && st.run) {
      const b = L.bbox(room.outline), lo = Math.min(st.run.from, st.run.to), hi = Math.max(st.run.from, st.run.to);
      for (let v = lo + 0.28; v < hi - 0.05; v += 0.28) out += st.run.axis === 'x' ? line([v, b.y0], [v, b.y1], 'tread') : line([b.x0, v], [b.x1, v], 'tread');
    }
    const [p, q] = lower ? [st.foot, st.top] : [st.top, st.foot];
    return out + line(p, q, 'stair', 'marker-end="url(#mArrow)"') + text(p, lower ? 'Up' : 'Down', 'small', -6);
  }
  function roomLabel(r) {
    const b = L.bbox(r.outline);
    if ((b.x1 - b.x0) * view.s < 50 || (b.y1 - b.y0) * view.s < 28) return '';
    const c = L.centroid(r.outline);
    return text(c, r.name || r.id, 'rname', 0) + text(c, area(L.polyArea(r.outline)), 'small', 14);
  }
  function handlesSvg() {
    const it = sel && sel.fi === fi ? L.itemOf(doc, sel) : null;
    if (!it || !editable()) return '';
    const pts = sel.kind === 'room' || sel.kind === 'object' ? it.outline : sel.kind === 'aperture' ? [it.a, it.b] : [];
    return pts.map((p) => `<rect x="${r1(X(p[0]) - 4)}" y="${r1(Y(p[1]) - 4)}" width="8" height="8" class="h"/>`).join('');
  }
  function render() {
    if (!doc || !active) return;
    measure();
    const f = floor(), out = [`<defs><marker id="mArrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7"
      orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#f59e0b"/></marker></defs>`];
    if (f.underlay) out.push(underlaySvg(f.underlay));
    if (grid && editable()) out.push(gridSvg());
    if (fi > 0) for (const r of doc.floors[fi - 1].rooms) out.push(`<polygon points="${poly(r.outline)}" class="below"/>`);
    for (const r of f.rooms) {
      out.push(`<polygon points="${poly(r.outline)}" fill="${KIND_FILL[r.kind] || KIND_FILL.room}"
        fill-opacity="${isSel('room', r.id) ? 0.32 : r.kind === 'outdoor' ? 0.06 : 0.13}"/>`);
    }
    for (const e of L.wallEdges(f)) out.push(line(e.a, e.b, e.outdoor ? 'wall out' : e.exterior ? 'wall ext' : 'wall int'));
    (f.open_edges || []).forEach((e, i) => out.push(line(e.a, e.b, 'open' + (isSel('open', e.id ?? i) ? ' sel' : ''))));
    for (const a of f.apertures || []) out.push(apertureSvg(a));
    for (const o of f.objects || []) out.push(objectSvg(o));
    for (const st of doc.stairs || []) out.push(stairsSvg(st, f));
    for (const r of f.rooms) out.push(roomLabel(r));
    if (sel?.kind === 'room' && sel.fi === fi) {
      const r = L.itemOf(doc, sel);
      if (r) out.push(`<polygon points="${poly(r.outline)}" fill="none" stroke="#60a5fa" stroke-width="2"/>`);
    }
    out.push(receiversSvg());
    out.push(handlesSvg());
    out.push(toolSvg());
    svg.innerHTML = out.join('');
  }

  // ---------- the tools ----------
  // Snapping targets on this floor: room and object corners (and the floor below's, to line floors up) and room
  // edges, leaving out the item being edited.
  function targets(skip) {
    const f = floor(), vertices = [], edges = [];
    for (const r of f.rooms) {
      if (skip && skip.kind === 'room' && skip.id === r.id) continue;
      vertices.push(...r.outline); edges.push(...L.edgesOf(r.outline));
    }
    for (const o of f.objects || []) if (!(skip && skip.kind === 'object' && skip.id === o.id)) vertices.push(...o.outline);
    if (fi > 0) for (const r of doc.floors[fi - 1].rooms) vertices.push(...r.outline);
    return { vertices, edges };
  }
  const snapAt = (p, skip, extra = {}) =>
    L.snap(p, { grid: grid ? gridStep() : 0, tol: 10 / view.s, align: true, ...targets(skip), ...extra }).p;
  // How far to move an outline so one of its corners lands on a nearby corner, else on a wall, else the grid.
  function snapMove(outline, dx, dy, skip) {
    const tol = 10 / view.s, t = targets(skip);
    let best = null;
    for (const v of outline) {
      const q = [v[0] + dx, v[1] + dy];
      for (const w of t.vertices) { const d = Math.hypot(w[0] - q[0], w[1] - q[1]); if (d <= tol && (!best || d < best.d)) best = { d, dx: dx + w[0] - q[0], dy: dy + w[1] - q[1] }; }
    }
    if (!best) {
      for (const v of outline) {
        const q = [v[0] + dx, v[1] + dy];
        for (const [a, b] of t.edges) { const s = L.segProject(q, a, b); if (s.dist <= tol && (!best || s.dist < best.d)) best = { d: s.dist, dx: dx + s.point[0] - q[0], dy: dy + s.point[1] - q[1] }; }
      }
    }
    if (best) return [best.dx, best.dy];
    if (!grid) return [dx, dy];
    const g = gridStep(), v = outline[0];
    return [Math.round((v[0] + dx) / g) * g - v[0], Math.round((v[1] + dy) / g) * g - v[1]];
  }

  // Select: a click picks the topmost thing; dragging a selected room, or any object, moves it; dragging a corner of
  // the selected room or object moves the corner; a drag anywhere else pans.
  const select = {
    down(p, e) {
      const tol = 8 / view.s, it = sel && sel.fi === fi ? L.itemOf(doc, sel) : null;
      const rid = L.receiverAt(doc, fi, p, 10 / view.s);
      if (rid) {
        sel = L.refOf('receiver', fi, rid);
        if (appliedRx(rid) === undefined) { toast('The live house has not loaded yet: try again in a moment'); render(); return renderPanel(); }
        drag = { kind: 'rx', ref: sel, start: doc, moved: false, sx: e.clientX, sy: e.clientY };
        render(); renderPanel();
        return;
      }
      if (it && sel.kind === 'aperture') {
        const end = [it.a, it.b].find((q) => Math.hypot(q[0] - p[0], q[1] - p[1]) <= tol);
        const on = L.hitTest(doc, fi, p, tol);
        if (end || (on && on.kind === 'aperture' && on.id === sel.id)) {
          drag = { kind: end ? 'apend' : 'apbody', ref: sel, from: end || p, start: doc, moved: false, sx: e.clientX, sy: e.clientY };
          return;
        }
      }
      if (it && (sel.kind === 'room' || sel.kind === 'object')) {
        const vi = it.outline.findIndex((v) => Math.hypot(v[0] - p[0], v[1] - p[1]) <= tol);
        if (vi >= 0) { drag = { kind: 'vertex', ref: sel, vi, start: doc, moved: false }; return; }
      }
      const hit = L.hitTest(doc, fi, p, tol), was = sel;
      if (hit && (hit.kind === 'object' || (hit.kind === 'room' && was && was.kind === 'room' && was.id === hit.id && was.fi === fi))) {
        sel = hit; drag = { kind: 'move', ref: hit, p0: p, start: doc, moved: false, sx: e.clientX, sy: e.clientY };
      } else {
        drag = { kind: 'pan', sx: e.clientX, sy: e.clientY, cx: view.cx, cy: view.cy, moved: false, hit };
      }
      render(); renderPanel();
    },
    move(p, e) {
      if (!drag) return;
      if (drag.kind === 'move') {
        if (!drag.moved && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 4) return;
        drag.moved = true;
        const it = L.itemOf(drag.start, drag.ref), [dx, dy] = snapMove(it.outline, p[0] - drag.p0[0], p[1] - drag.p0[1], drag.ref);
        doc = drag.ref.kind === 'room' ? L.moveRoom(drag.start, fi, drag.ref.id, dx, dy)
                                       : L.updateItem(drag.start, drag.ref, { outline: L.moveOutline(it.outline, dx, dy) });
        render();
      } else if (drag.kind === 'rx') {
        if (!drag.moved && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 3) return;
        drag.moved = true;
        const s = rxSnap(p);
        doc = L.moveReceiver(drag.start, drag.ref.id, fi, s.p, appliedRx(drag.ref.id), L.isoLocal(), s.height);
        render();
      } else if (drag.kind === 'apend' || drag.kind === 'apbody') {
        if (!drag.moved && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 3) return;
        drag.moved = true;
        doc = L.dragAperture(drag.start, drag.ref, drag.kind === 'apend' ? 'end' : 'body', p, drag.from);
        render();
      } else if (drag.kind === 'vertex') {
        drag.moved = true;
        const it = L.itemOf(drag.start, drag.ref), q = snapAt(p, drag.ref, { vertices: [...targets(drag.ref).vertices, ...it.outline.filter((_, i) => i !== drag.vi)] });
        doc = L.updateItem(drag.start, drag.ref, { outline: it.outline.map((v, i) => (i === drag.vi ? q : v)) });
        render();
      }
    },
    up() {
      const d = drag; drag = null;
      if (d && d.kind === 'pan' && !d.moved) { sel = d.hit; render(); renderPanel(); return; }
      if (d && d.moved && ['move', 'vertex', 'apend', 'apbody', 'rx'].includes(d.kind)) { const nd = doc; doc = d.start; commit(nd); }
      if (d && (d.kind === 'apend' || d.kind === 'apbody')) renderPanel();
    },
  };
  // ---------- the drawing tools ----------
  // Each tool: down / move / up with the pointer in metres, an optional key handler (true = handled), svg() for its
  // preview, cancel(). The new thing is selected so its properties can be typed straight away.
  let cur = null;                                                 // the snapped cursor, for previews
  const r6 = (v) => Math.round(v * 1e6) / 1e6;
  const dist = (a, b) => Math.hypot(b[0] - a[0], b[1] - a[1]);
  const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  const sizeLabel = (a, b) => `${fmt(Math.abs(b[0] - a[0]))} x ${fmt(Math.abs(b[1] - a[1]))}`;
  const rectSvg = (a, b) => `<polygon points="${poly(L.rectOutline(a, b))}" class="tool"/>` + text(mid(a, b), sizeLabel(a, b), 'tlabel', 4);
  function place(nd, ref) { sel = ref; commit(nd); }

  // A rectangle dragged out, snapped at both corners: objects and stairs.
  function dragRect(hint, make) {
    let a = null, b = null;
    return {
      hint,
      down(p) { a = b = snapAt(p); },
      move(p) { if (a) { b = snapAt(p); render(); } },
      up(p) {
        if (!a) return;
        const A = a, B = snapAt(p);
        a = b = null;
        if (Math.abs(B[0] - A[0]) < 0.1 || Math.abs(B[1] - A[1]) < 0.1) { render(); return toast('Drag out a rectangle'); }
        make(A, B);
      },
      svg: () => (a && b ? rectSvg(a, b) : ''),
      cancel() { a = b = null; },
      key(e) { if (e.key === 'Escape' && a) { a = b = null; render(); return true; } return false; },
    };
  }

  // Room: drag a rectangle, or click corners (a click on the first corner, a double-click or Enter closes it). Typing
  // a size ("12' x 10'", "4.2 x 3.6") and Enter makes a rectangle from the first corner (or the cursor) towards the
  // cursor; a length and Enter adds the next side along the cursor's direction. Sides are straight unless Shift.
  const room = (() => {
    let down = null, pts = [], typed = '', raw = null;          // raw: the cursor before snapping, for directions
    const reset = () => { down = null; pts = []; typed = ''; };
    function finish(outline) {
      reset();
      if (outline.length < 3 || L.polyArea(outline) < 0.05) { render(); return toast('A room needs some floor area'); }
      const nd = L.addRoom(doc, fi, outline);
      place(nd, L.refOf('room', fi, nd.floors[fi].rooms.at(-1).id));
    }
    function close() {
      const o = pts.filter((q, i) => i === 0 || dist(q, pts[i - 1]) > 1e-6);
      if (o.length > 1 && dist(o[0], o.at(-1)) < 1e-6) o.pop();
      finish(o);
    }
    function along(from, free) {
      const t = raw || from, dx = t[0] - from[0], dy = t[1] - from[1];
      if (free) { const n = Math.hypot(dx, dy) || 1; return [dx / n, dy / n]; }
      return Math.abs(dx) >= Math.abs(dy) ? [Math.sign(dx) || 1, 0] : [0, Math.sign(dy) || 1];
    }
    function applyTyped(e) {
      const size = L.parseSize(typed), len = L.parseLength(typed);
      if (size && pts.length <= 1) {
        const a = pts[0] || cur;
        if (!a) return;
        const sx = raw && raw[0] < a[0] ? -1 : 1, sy = raw && raw[1] < a[1] ? -1 : 1;
        return finish(L.rectOutline(a, [a[0] + sx * size[0], a[1] + sy * size[1]]));
      }
      if (len && pts.length) {
        const a = pts.at(-1), d = along(a, e.shiftKey);
        pts.push([r6(a[0] + d[0] * len), r6(a[1] + d[1] * len)]); typed = '';
        return render();
      }
      toast(pts.length ? `Type a length like 12' 6", or a size like 12' x 10'` : `Type a size like 12' x 10' or 4.2 x 3.6`);
    }
    const snapNext = (p, e) => snapAt(p, null, pts.length ? { from: pts.at(-1), ortho: !e.shiftKey } : {});
    return {
      hint: `Drag a rectangle, or click its corners. Type a size like 12' x 10' and press Enter.`,
      down(p, e) { down = { p: snapNext(p, e), s: [e.clientX, e.clientY], moved: false }; },
      move(p, e) {
        raw = p; cur = snapNext(p, e);
        if (down && !pts.length && Math.hypot(e.clientX - down.s[0], e.clientY - down.s[1]) >= 4) down.moved = true;
        render();
      },
      up(p) {
        if (!down) return;
        const d = down;
        down = null;
        if (d.moved) return finish(L.rectOutline(d.p, snapAt(p)));
        if (pts.length >= 3 && dist(d.p, pts[0]) <= 10 / view.s) return close();
        pts.push(d.p); render();
      },
      dbl() { if (pts.length >= 3) close(); },
      svg() {
        let o = '';
        if (down?.moved && cur) o += rectSvg(down.p, cur);
        if (pts.length) {
          o += `<polyline points="${poly(cur ? [...pts, cur] : pts)}" class="tool" style="fill:none"/>` +
            `<circle cx="${r1(X(pts[0][0]))}" cy="${r1(Y(pts[0][1]))}" r="6" class="h"/>`;
          if (cur) o += text(mid(pts.at(-1), cur), fmt(dist(pts.at(-1), cur)), 'tlabel', -8);
        }
        if (typed && cur) o += text(cur, typed + ' (Enter)', 'tlabel', -18);
        return o;
      },
      cancel: reset,
      key(e) {
        if (e.key === 'Escape') { if (pts.length || typed || down) { reset(); render(); return true; } return false; }
        if (e.key === 'Enter') { if (typed) applyTyped(e); else if (pts.length >= 3) close(); return true; }
        if (e.key === 'Backspace') {
          if (typed) typed = typed.slice(0, -1); else if (pts.length) pts.pop(); else return false;
          render(); return true;
        }
        if (e.key.length === 1 && (typed || pts.length ? /[0-9.'"xX×mMcC½ ]/ : /[0-9.]/).test(e.key)) { typed += e.key; render(); return true; }
        return false;
      },
    };
  })();

  // Door, window, garage door, doorway: hovering lights up the nearest wall and where it would go; a click adds it.
  function apertureTool(kind) {
    let hit = null;
    const find = (p) => L.nearestEdge(doc, fi, p, Math.max(0.3, 12 / view.s));
    return {
      hint: `Click a wall to add a ${AP_KINDS[kind].toLowerCase()}; type its width in the panel.`,
      down() {},
      move(p) { hit = find(p); render(); },
      up(p) {
        hit = find(p);
        if (!hit) return toast('Click on a wall');
        const nd = L.addAperture(doc, fi, kind, hit);
        place(nd, L.refOf('aperture', fi, nd.floors[fi].apertures.at(-1).id));
      },
      svg() {
        if (!hit) return '';
        const len = dist(hit.a, hit.b), w = Math.min(L.APERTURE_WIDTH[kind], len), c = Math.max(w / 2, Math.min(len - w / 2, hit.t * len));
        const u = [(hit.b[0] - hit.a[0]) / len, (hit.b[1] - hit.a[1]) / len];
        const at = (t) => [hit.a[0] + u[0] * t, hit.a[1] + u[1] * t];
        return line(hit.a, hit.b, 'hot') + line(at(c - w / 2), at(c + w / 2), 'ap sel');
      },
      cancel() { hit = null; },
    };
  }

  // No wall: hovering lights up a wall two rooms share; a click opens it (an open-plan or archway join).
  const nowall = (() => {
    let pair = null;
    function find(p) {
      const f = floor(), tol = Math.max(0.2, 10 / view.s);
      let best = null;
      for (let i = 0; i < f.rooms.length; i++) {
        for (let j = i + 1; j < f.rooms.length; j++) {
          const sg = L.sharedSegment(f.rooms[i], f.rooms[j]);
          if (!sg) continue;
          const d = L.segProject(p, sg.a, sg.b).dist;
          if (d <= tol && (!best || d < best.d)) best = { d, a: f.rooms[i].id, b: f.rooms[j].id, seg: sg };
        }
      }
      return best;
    }
    return {
      hint: 'Click a wall between two rooms to take it away (open plan). For an opening in a wall, use Doorway.',
      down() {},
      move(p) { pair = find(p); render(); },
      up(p) {
        pair = find(p);
        if (!pair) return toast('Click on a wall two rooms share');
        if ((floor().open_edges || []).some((e) => (e.rooms || []).includes(pair.a) && (e.rooms || []).includes(pair.b))) return toast('That wall is already open');
        const nd = L.addOpenEdge(doc, fi, pair.a, pair.b);
        place(nd, L.refOf('open', fi, nd.floors[fi].open_edges.at(-1).id));
      },
      svg: () => (pair ? line(pair.seg.a, pair.seg.b, 'hot') : ''),
      cancel() { pair = null; },
    };
  })();

  // Wall: a wall inside a room (a half wall, a partition) dragged as a line, straight unless Shift.
  const wall = (() => {
    let a = null, b = null;
    return {
      hint: 'Drag a wall inside a room, like a partition or a half wall (straight unless Shift is held).',
      down(p) { a = b = snapAt(p); },
      move(p, e) { if (a) { b = snapAt(p, null, { from: a, ortho: !e.shiftKey }); render(); } },
      up(p, e) {
        if (!a) return;
        const A = a, B = snapAt(p, null, { from: a, ortho: !e.shiftKey });
        a = b = null;
        if (dist(A, B) < 0.1) { render(); return toast('Drag out the wall'); }
        const nd = L.addWall(doc, fi, A, B);
        place(nd, L.refOf('object', fi, nd.floors[fi].objects.at(-1).id));
      },
      svg: () => (a && b ? line(a, b, 'hot') + text(mid(a, b), fmt(dist(a, b)), 'tlabel', -8) : ''),
      cancel() { a = b = null; },
      key(e) { if (e.key === 'Escape' && a) { a = b = null; render(); return true; } return false; },
    };
  })();

  // Measure: two clicks; the distance stays until Esc or the next click.
  const measureTool = (() => {
    let a = null, b = null;
    return {
      hint: 'Click two points to measure between them.',
      down() {},
      move() { if (a && !b) render(); },
      up(p) { const q = snapAt(p); if (!a || b) { a = q; b = null; } else b = q; render(); },
      svg() {
        const e = b || (a && cur);
        if (!a || !e) return '';
        return line(a, e, 'hot') + text(mid(a, e), `${fmt(dist(a, e))}   (${sizeLabel(a, e)})`, 'tlabel', -8);
      },
      cancel() { a = b = null; },
      key(e) { if (e.key === 'Escape' && a) { a = b = null; render(); return true; } return false; },
    };
  })();

  // Image: drag the floor's underlay to move it (unless locked); Set scale in the panel arms two clicks on the image,
  // then the panel asks how far apart they really are and the image scales about the first point.
  const imageTool = (() => {
    let mv = null;
    return {
      scale: null,
      hint: 'Drag the image to line it up. Set its scale and lock it in the panel.',
      down(p) {
        const u = floor().underlay;
        if (!u) return toast('Load an image in the panel first');
        if (this.scale) return;
        if (u.locked) return toast('The image is locked: unlock it in the panel to move it');
        mv = { p0: p, start: doc, x: u.x, y: u.y };
      },
      move(p) {
        if (mv) {
          const nd = structuredClone(mv.start), u = nd.floors[fi].underlay;
          u.x = r6(mv.x + p[0] - mv.p0[0]); u.y = r6(mv.y + p[1] - mv.p0[1]);
          doc = nd; render();
        } else if (this.scale?.pts.length === 1) render();
      },
      up(p) {
        if (this.scale) {
          if (this.scale.pts.length < 2) this.scale.pts.push(p);
          render(); renderPanel();
          if (this.scale.pts.length === 2) panel.querySelector('[data-k=ulreal]')?.focus();
          return;
        }
        if (!mv) return;
        const m = mv;
        mv = null;
        if (doc !== m.start) { const nd = doc; doc = m.start; commit(nd); }
      },
      svg() {
        const sc = this.scale;
        if (!sc || !sc.pts.length) return '';
        const e = sc.pts[1] || cur;
        return e ? line(sc.pts[0], e, 'hot') + text(mid(sc.pts[0], e), fmt(dist(sc.pts[0], e)) + ' on the image', 'tlabel', -8) : '';
      },
      cancel() { if (mv) doc = mv.start; mv = null; this.scale = null; },
      key(e) {
        if (e.key !== 'Escape' || !(this.scale || mv)) return false;
        this.cancel(); render(); renderPanel(); return true;
      },
    };
  })();
  function applyScale(real) {
    const sc = imageTool.scale, f = floor();
    if (!sc || sc.pts.length < 2 || !f.underlay) return;
    const drawn = dist(sc.pts[0], sc.pts[1]);
    if (drawn < 1e-6) return toast('Click two different points');
    const k = real / drawn, p0 = sc.pts[0], nd = structuredClone(doc), u = nd.floors[fi].underlay;
    u.m_per_px = r6(u.m_per_px * k);
    u.x = r6(p0[0] + k * (u.x - p0[0])); u.y = r6(p0[1] + k * (u.y - p0[1]));   // the first point stays put
    imageTool.scale = null;
    commit(nd);
    toast('Scale set');
  }
  async function loadImage(file) {
    const fid = floor().id;                                           // the floor shown may change while it uploads
    if (file.size > 15 * 2 ** 20) return toast('That image is over 15 MB: save it smaller and load it again');
    try {
      const r = await fetch('api/house/underlay?floor=' + encodeURIComponent(fid), { method: 'POST', headers: { 'Content-Type': file.type }, body: file });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status);
      imgVer[j.file] = Date.now();
      const im = new Image();
      await new Promise((ok, fail) => { im.onload = ok; im.onerror = () => fail(new Error('the image did not load')); im.src = underlayUrl(j.file); });
      const old = doc.floors.find((g) => g.id === fid)?.underlay;
      const nd = L.setUnderlay(doc, fid, j.file, [im.naturalWidth, im.naturalHeight], { cx: view.cx, cy: view.cy, w: (W * 0.7) / view.s });
      if (!nd) return toast('That floor was deleted while its image loaded');
      commit(nd);
      toast(old ? 'Image replaced' : 'Image loaded: now set its scale');
    } catch (e) { toast('Could not load the image: ' + e.message); }
  }
  fileIn.addEventListener('change', () => { const f = fileIn.files[0]; fileIn.value = ''; if (f) loadImage(f); });

  // ---------- the 3-D preview: the server's build of the draft, a second after each save ----------
  const roomRef = () => (sel && sel.kind === 'room' ? sel : null);
  async function showPreview(on) {
    previewOn = on; local.set('mapPreview', on ? '1' : '0');
    $('mapPrev').classList.toggle('on', on);
    $('mapPreview').style.display = on ? '' : 'none';
    fit(); render();                                                 // the plan just changed height
    if (on && !preview) {
      try { preview = (await import('./map_preview.js')).initPreview($('mapPreview')); }
      catch (e) { $('mapPreview').textContent = 'The 3-D preview could not start: ' + e.message; return; }
    }
    preview?.show(on && active);
    if (on) refreshPreview();
  }
  function schedulePreview() { if (previewOn) { clearTimeout(previewT); previewT = setTimeout(refreshPreview, 1000); } }
  async function refreshPreview() {
    if (!previewOn || !preview) return;
    const seq = ++previewSeq;
    let pv;
    try { pv = await api('GET', 'api/house/draft/preview'); } catch (e) { pv = { error: e.message }; }
    if (seq === previewSeq) preview.update(pv, roomRef());
  }
  $('mapPrev').onclick = () => showPreview(!previewOn);

  // ---------- receivers (spec 2026-10-01): proxies and Echo Shows on the map ----------
  let appliedDoc = null, outlets = [], antenna = 0, unplaced = {}, rxStatus = {}, toPlace = null;
  // the live house's receiver; null when it is not in the live house (placed in this draft); undefined when the live
  // house is not known yet - then nothing that needs it (a move's stamp, removal) is done
  const appliedRx = (id) => (appliedDoc ? (appliedDoc.receivers || []).find((r) => r.id === id) || null : undefined);
  const isEcho = (r) => /Show/.test(r.name || '');
  // what Home Assistant hears changes under the editor (a proxy plugged in, the engine restarted): kept fresh
  setInterval(() => { if (active && doc && !drag) loadReceiverInfo(); }, 20000);
  async function loadReceiverInfo() {
    const get = (path) => api('GET', path).catch(() => null);
    const [a, o, c, s] = await Promise.all([get('api/house/doc'), get('api/outlets'), get('api/ingest/compare'), get('api/receivers')]);
    appliedDoc = a || appliedDoc;                                   // a failed poll keeps what was known
    outlets = (o && o.outlets) || []; antenna = (o && o.antenna_offset_m) || 0; unplaced = (c && c.unplaced) || {};
    rxStatus = Object.fromEntries(((s && s.receivers) || []).map((r) => [r.name, r]));
    const typing = panel.contains(document.activeElement) && document.activeElement.matches('input, select');
    if (active && doc && !typing) { render(); if (!sel || sel.kind === 'receiver') renderPanel(); }
  }
  // a measured outlet within 30 cm (taking its height plus the antenna offset), else the walls and the grid
  function rxSnap(p) {
    const o = L.snapToOutlet(p, fi, outlets, Math.max(0.3, 10 / view.s), antenna);
    return o ? { p: o.p, height: o.height } : { p: snapAt(p), height: null };
  }
  function receiversSvg() {
    const fid = floor().id;
    return (doc.receivers || []).filter((r) => r.floor === fid).map((r) => {
      const st = rxStatus[r.name], off = st && (!st.enabled || !st.alive), on = isSel('receiver', r.id);
      const x = r1(X(r.x)), y = r1(Y(r.y)), w = isEcho(r) ? 8 : 6, h = isEcho(r) ? 6 : 6;
      return `<rect x="${x - w}" y="${y - h}" width="${2 * w}" height="${2 * h}" rx="${isEcho(r) ? 3 : 1}"
        class="rx${isEcho(r) ? ' echo' : ''}${off ? ' off' : ''}${on ? ' sel' : ''}"/>` + text([r.x, r.y], r.name, 'small rxname', 18);
    }).join('');
  }
  const receiverTool = (() => {
    let at = null;
    return {
      hint: 'Click where the receiver is; it snaps to a measured outlet nearby.',
      down() {},
      move(p) { at = rxSnap(p); render(); },
      up(p) {
        if (!toPlace) return toast('Choose a receiver in the panel first (Heard, not placed, or New receiver)');
        const s = rxSnap(p), nd = L.addReceiver(doc, fi, s.p, { name: toPlace.name, address: toPlace.address,
                                                                height: s.height ?? 0.3, added: L.isoLocal() });
        toPlace = null;
        place(nd, L.refOf('receiver', fi, nd.receivers.at(-1).id));
        setTool('select');
      },
      svg() {
        if (!at) return '';
        return `<rect x="${r1(X(at.p[0])) - 6}" y="${r1(Y(at.p[1])) - 6}" width="12" height="12" class="rx ghost"/>` +
          (toPlace ? text(at.p, toPlace.name, 'tlabel', -12) : '');
      },
      cancel() { at = null; },
    };
  })();
  function receiversHtml(f) {
    const mine = (doc.receivers || []).filter((r) => r.floor === f.id), others = (doc.receivers || []).length - mine.length;
    const known = new Set((doc.receivers || []).flatMap((r) => [r.address, r.mac].filter(Boolean).map((a) => a.toLowerCase())));
    const macPlus2 = (m) => m.replace(/[0-9a-f]{2}$/i, (x) => ((parseInt(x, 16) + 2) % 256).toString(16).padStart(2, '0'));
    (doc.receivers || []).forEach((r) => { if (r.mac) known.add(macPlus2(r.mac.toLowerCase())); });
    const heard = Object.entries(unplaced).filter(([a]) => !known.has(a.toLowerCase()));
    const roomName = (r) => f.rooms.find((x) => x.id === r.room)?.name || 'outside every room';
    let h = `<h3>Receivers</h3>` + (mine.length ? mine.map((r) => `<div class="mhist"><span>${esc(r.name)} - ${esc(roomName(r))},
      ${fmt(r.height)} up</span><button data-act="rxsel" data-id="${esc(r.id)}">Show</button></div>`).join('')
      : '<p class="muted">None on this floor.</p>') + (others ? `<p class="muted">${others} on other floors.</p>` : '');
    if (!editable()) return h;
    if (heard.length) {
      h += `<p class="muted">Heard by Home Assistant, not on the map:</p>` + heard.map(([a, n]) =>
        `<div class="mhist"><span>${esc(n)}</span><button data-act="rxplace" data-addr="${esc(a)}" data-name="${esc(n)}">Place</button></div>`).join('');
    }
    return h + `<div class="mrow"><label>New receiver</label><div class="ctl"><input data-k="rxname" placeholder="its name in Home Assistant">
      <button data-act="rxnew">Place</button></div></div>`;
  }
  function receiverForm(r) {
    const live = appliedRx(r.id), f = doc.floors[floorIndex(r.floor)], st = rxStatus[r.name];
    const room = f?.rooms.find((x) => x.id === r.room)?.name || 'outside every room';
    const pm = L.pendingMove(r, live), pe = L.pendingEpochs(r, live);
    const dt = (iso) => (iso || '').slice(0, 16);
    let h = `<h3>${isEcho(r) ? 'Echo Show' : 'Receiver'} ${editable() && !live ? '<button data-act="del">Remove</button>' : ''}</h3>` +
      (live ? `<div class="mrow"><label>Name</label><div>${esc(r.name)}<div class="muted mhint">The name Home Assistant gives it:
        its captures are filed under it.</div></div></div>` : field('Name', 'name', r.name, { hint: 'The name Home Assistant gives it.' })) +
      `<div class="mrow"><label>Room</label><div>${esc(room)}</div></div>` +
      field('Floor', 'rxfloor', r.floor, { opts: Object.fromEntries(doc.floors.map((x) => [x.id, x.name])),
                                          hint: 'Moving it to another floor keeps its spot on the plan.' }) +
      field('Height', 'rxh', fmt(r.height), { t: 'len', hint: 'Above the floor. Drag it on the map to move it.' }) +
      (editable() ? `<div class="ctl">${Object.entries(L.HEIGHT_PRESETS).map(([n, v]) =>
        `<button data-act="rxheight" data-h="${v}">${esc(n)}</button>`).join('')}</div>` : '') +
      (st ? `<div class="mrow"><label>Now</label><div>${st.enabled ? (st.alive ? 'heard' : 'not heard') : 'switched off'}</div></div>` : '') +
      field('Model', 'model', r.model || '', { ph: 'e.g. esp32-s3 (XIAO)' }) +
      field('Wi-Fi MAC', 'mac', r.mac || '', { ph: 'for an ESPHome proxy' }) +
      field('Bluetooth address', 'address', r.address || '', { ph: 'as the bridge reports it' });
    h += '<h3>Moves</h3>';
    if (pm) {
      h += `<p>Moved in this draft. When did it move?</p>` +
        (editable() ? `<div class="mrow"><label>Moved at</label><input type="datetime-local" data-k="rxmoved" value="${esc(dt(pm.at))}"></div>
          <div class="ctl"><button data-act="rxback" title="Back to where the live house has it">Put back</button></div>` : '');
    }
    const old = (r.moves || []).filter((m) => m !== pm);
    h += old.length ? old.map((m) => `<p class="muted">Until ${esc(m.at)}: ${fmt(m.x)}, ${fmt(m.y)}</p>`).join('')
      : (pm ? '' : '<p class="muted">Never moved.</p>');
    h += `<h3>Re-seated, re-oriented or swapped</h3>` + ((r.epochs || []).map((e) => pe.includes(e) && editable()
      ? `<div class="mhist"><input type="datetime-local" data-k="rxepochat" data-at="${esc(e)}" value="${esc(dt(e))}">
         <button data-act="rxdelepoch" data-at="${esc(e)}">Remove</button></div>`
      : `<div class="mhist"><span>${esc(e)}</span></div>`).join('') ||
      '<p class="muted">Never.</p>') + (editable() ? `<div class="ctl"><button data-act="rxepoch">It was re-seated, re-oriented or swapped now</button></div>` : '');
    if (r.note) h += `<p class="muted">${esc(r.note)}</p>`;
    return h;
  }

  const tools = {
    select, room, nowall, wall, measure: measureTool, image: imageTool, receiver: receiverTool,
    door: apertureTool('door'), window: apertureTool('window'), garage_door: apertureTool('garage_door'), opening: apertureTool('opening'),
    object: dragRect('Drag out the object, then choose what it is in the panel.', (A, B) => {
      const nd = L.addObject(doc, fi, L.rectOutline(A, B), 'Other');
      place(nd, L.refOf('object', fi, nd.floors[fi].objects.at(-1).id));
    }),
    stairs: dragRect('Drag the stairs from the bottom step towards the top: they climb in the direction you drag.', (A, B) => {
      const dx = B[0] - A[0], dy = B[1] - A[1], dir = Math.abs(dx) >= Math.abs(dy) ? (dx > 0 ? '+x' : '-x') : (dy > 0 ? '+y' : '-y');
      try {
        const nd = L.addStairs(doc, fi, L.rectOutline(A, B), dir);
        place(nd, L.refOf('room', fi, nd.stairs.at(-1).lower.room));
      } catch (e) { render(); toast(e.message); }
    }),
  };
  function toolSvg() { return tools[tool]?.svg ? tools[tool].svg() : ''; }
  function setTool(t) {
    if (!tools[t]) return;
    tools[tool]?.cancel?.();
    tool = t; drag = null;
    if (t === 'image' && sel) { sel = null; renderPanel(); }
    root.querySelectorAll('#mapTools button').forEach((b) => b.classList.toggle('on', b.dataset.t === tool));
    svg.style.cursor = tool === 'select' ? 'default' : 'crosshair';
    $('mapHint').textContent = tools[tool].hint || '';
    render(); renderPanel();
  }
  root.querySelectorAll('#mapTools button').forEach((b) => {
    b.disabled = !tools[b.dataset.t];
    b.onclick = () => setTool(b.dataset.t);
  });

  svg.addEventListener('pointerdown', (e) => {
    if (!doc) return;
    if (e.button === 1 || e.button === 2 || (e.button === 0 && spaceDown) || (e.button === 0 && !editable())) {
      drag = { kind: 'pan', sx: e.clientX, sy: e.clientY, cx: view.cx, cy: view.cy, moved: false,
               hit: e.button === 0 ? L.hitTest(doc, fi, toM(e), 8 / view.s) : sel };
    } else if (e.button === 0) tools[tool].down(toM(e), e);
    svg.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  svg.addEventListener('pointermove', (e) => {
    if (!doc) return;
    if (drag?.kind === 'pan') {
      if (!drag.moved && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 4) return;
      drag.moved = true; svg.style.cursor = 'grabbing';
      view.cx = drag.cx - (e.clientX - drag.sx) / view.s; view.cy = drag.cy + (e.clientY - drag.sy) / view.s;
      render(); return;
    }
    if (tool !== 'select') cur = snapAt(toM(e));
    tools[tool].move(toM(e), e);
  });
  svg.addEventListener('pointerleave', () => { if (tool !== 'select' && !drag) { cur = null; render(); } });
  svg.addEventListener('dblclick', () => tools[tool].dbl?.());
  svg.addEventListener('pointerup', (e) => {
    if (!doc) return;
    if (drag?.kind === 'pan') {
      svg.style.cursor = tool === 'select' ? 'default' : 'crosshair';
      if (tool !== 'select' || !editable()) { const d = drag; drag = null; if (!d.moved && e.button === 0) { sel = d.hit; render(); renderPanel(); } return; }
    }
    tools[tool].up(toM(e), e);
  });
  function abandon() {
    if (drag && drag.start && ['move', 'vertex', 'apend', 'apbody', 'rx'].includes(drag.kind)) doc = drag.start;
    drag = null;
    tools[tool].cancel?.();
    svg.style.cursor = tool === 'select' ? 'default' : 'crosshair';
    render();
  }
  svg.addEventListener('pointercancel', abandon);
  svg.addEventListener('lostpointercapture', () => { if (drag) abandon(); });   // after a pointerup, drag is already null
  svg.addEventListener('contextmenu', (e) => e.preventDefault());
  svg.addEventListener('wheel', (e) => {
    if (!doc) return;
    e.preventDefault();
    const p = toM(e), b = svg.getBoundingClientRect();
    view.s = Math.max(4, Math.min(1500, view.s * Math.exp(-e.deltaY * 0.0015)));
    view.cx = p[0] - (e.clientX - b.left - W / 2) / view.s; view.cy = p[1] + (e.clientY - b.top - H / 2) / view.s;
    render();
  }, { passive: false });
  addEventListener('keydown', (e) => {
    if (!active || !doc || e.target.closest?.('input, select, textarea')) return;
    const k = e.key, mod = e.ctrlKey || e.metaKey;
    if (mod && k.toLowerCase() === 'z' && editable()) { e.preventDefault(); return e.shiftKey ? redo() : undo(); }
    if (mod && k.toLowerCase() === 'y' && editable()) { e.preventDefault(); return redo(); }
    if (mod || e.altKey) return;
    if (tools[tool].key?.(e)) { e.preventDefault(); return; }     // first: a typed size has spaces in it
    if (k === ' ') { spaceDown = true; e.preventDefault(); return; }
    if (k === 'Escape') { if (tool !== 'select') setTool('select'); else if (sel) { sel = null; render(); renderPanel(); } return; }
    if ((k === 'Delete' || (k === 'Backspace' && tool === 'select')) && sel && editable()) { e.preventDefault(); return remove(); }
    if (k === 'f' || k === 'F') { fit(); return render(); }
    const t = TOOLS.find(([, key]) => key === k.toUpperCase());
    if (t && editable()) setTool(t[0]);
  });
  addEventListener('keyup', (e) => { if (e.key === ' ') spaceDown = false; });
  addEventListener('resize', () => { if (active && doc) { render(); renderPanel(); } });

  function remove() {
    if (sel.kind === 'receiver') {
      if (appliedRx(sel.id) === undefined) return toast('The live house has not loaded yet: try again in a moment');
      if (appliedRx(sel.id)) return toast('A receiver in the live house keeps its history: switch it off on the Receivers tab');
      const nd = L.removeReceiver(doc, sel.id, null);
      sel = null; return commit(nd);
    }
    const nd = L.deleteItem(doc, sel);
    sel = null; commit(nd);
  }

  // ---------- the top bar ----------
  function renderTop() {
    if (!doc) return;
    $('mapFloors').innerHTML = doc.floors.map((f, i) => `<button data-f="${i}" class="${i === fi ? 'on' : ''}">${esc(f.name)}</button>`).join('');
    $('mapFloors').querySelectorAll('button').forEach((b) => b.onclick = () => {
      fi = +b.dataset.f; local.set('mapFloor', floor().id);
      if (sel && sel.fi !== fi) sel = null;
      fit(); render(); renderTop(); renderPanel();
    });
    $('mapUnits').querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.u === units()));
    $('mapGrid').classList.toggle('on', grid);
    $('mapUndo').disabled = !hist.canUndo; $('mapRedo').disabled = !hist.canRedo;
    const errs = problems.filter((p) => p.severity === 'error').length;
    $('mapProbs').textContent = problems.length ? `${errs ? errs + ' to fix' : ''}${errs && problems.length > errs ? ', ' : ''}${problems.length > errs ? problems.length - errs + ' to check' : ''}` : 'No problems';
    $('mapProbs').className = errs ? 'bad' : problems.length ? 'warn' : '';
    const ab = $('mapApply');
    ab.disabled = !!applying || !hasDraft || errs > 0;
    ab.title = applying ? 'Applying the draft' : !hasDraft ? 'No changes to apply' : errs ? 'Fix the problems first'
      : copy.title;
    ab.textContent = applyArmed ? copy.confirm : 'Apply';
    ab.classList.toggle('armed', applyArmed); ab.classList.toggle('primary', !applyArmed);
  }
  $('mapAbove').onclick = () => { const nd = L.addFloor(doc, 'above', fi); fi += 1; sel = null; commit(nd); fit(); render(); };
  $('mapBelow').onclick = () => { const nd = L.addFloor(doc, 'below', fi); sel = null; commit(nd); fit(); render(); };
  $('mapUnits').querySelectorAll('button').forEach((b) => b.onclick = () => {
    unitsView = b.dataset.u; local.set('mapUnits', unitsView);
    render(); renderTop(); renderPanel();
  });
  $('mapGrid').onclick = () => { grid = !grid; local.set('mapGrid', grid ? '1' : '0'); renderTop(); render(); };
  $('mapFit').onclick = () => { fit(); render(); };
  $('mapUndo').onclick = undo; $('mapRedo').onclick = redo;
  $('mapProbs').onclick = () => { sel = null; render(); renderPanel(); };
  $('mapApply').onclick = () => {
    if (!applyArmed) {
      applyArmed = true; renderTop();
      setTimeout(() => { if (applyArmed) { applyArmed = false; renderTop(); } }, 4000);
      return;
    }
    applyArmed = false; renderTop(); startApply();
  };

  // ---------- the panel: the selection's properties, or the floor, its problems and the draft ----------
  const optList = (opts, v) => Object.entries(opts).map(([k, n]) => `<option value="${k}"${k === v ? ' selected' : ''}>${esc(n)}</option>`).join('');
  function field(label, k, value, { t = 'text', opts = null, ro = !editable(), hint = '', ph = '', data = '' } = {}) {
    let input;
    if (ro) input = `<span>${esc(opts ? opts[value] ?? value : value)}</span>`;
    else if (opts) input = `<select data-k="${k}" ${data}>${optList(opts, value)}</select>`;
    else input = `<input data-k="${k}" data-t="${t}" value="${esc(value ?? '')}" placeholder="${esc(ph)}" ${data} autocomplete="off">`;
    return `<div class="mrow"><label>${label}</label><div>${input}${hint ? `<div class="muted mhint">${hint}</div>` : ''}</div></div>`;
  }
  const lenOrBlank = (v) => (v == null ? '' : fmt(v));
  function roomForm(r) {
    const b = L.bbox(r.outline);
    let h = `<h3>Room ${editable() ? '<button data-act="del">Delete room</button>' : ''}</h3>` +
      field('Name', 'name', r.name, { data: 'list="mapHaAreas"', hint: haHint('room', r) }) + haList('mapHaAreas', areasFor()) +
      field('Kind', 'kind', r.kind, { opts: ROOM_KINDS }) +
      field('Step', 'step', r.step ? (r.step < 0 ? '-' : '') + fmt(Math.abs(r.step)) : '', { t: 'soff', ph: 'level with the floor', data: 'data-opt="1"',
        hint: 'How far its floor sits above (or, with a minus, below) the rest of the floor - a sunken garage is -7".' });
    if (L.isRect(r.outline)) h += field('Width', 'w', fmt(b.x1 - b.x0), { t: 'len' }) + field('Depth', 'd', fmt(b.y1 - b.y0), { t: 'len' });
    else h += L.edgesOf(r.outline).map(([a, c], i) => field(`Side ${i + 1}`, 'edge', fmt(Math.hypot(c[0] - a[0], c[1] - a[1])), { t: 'len', data: `data-i="${i}"` })).join('');
    h += `<div class="mrow"><label>Area</label><div>${area(L.polyArea(r.outline))}</div></div>`;
    const f = floor();
    for (const st of doc.stairs || []) {
      if (st.lower.floor === f.id && st.lower.room === r.id) {
        const up = doc.floors[floorIndex(st.upper.floor)];
        h += `<h3>Stairs</h3><div class="mrow"><label>Climbs to</label><div>${esc(up ? up.name : st.upper.floor)}</div></div>` +
          field('Climbing', 'dir', L.stairsDir(st), { opts: DIRS, data: `data-id="${esc(st.id)}"` });
      } else if (st.upper.floor === f.id && st.upper.room === r.id) {
        const lo = doc.floors[floorIndex(st.lower.floor)];
        h += `<h3>Stairs</h3><div class="mrow"><label>Comes up from</label><div>${esc(lo ? lo.name : st.lower.floor)}</div></div>`;
      }
    }
    return h;
  }
  function objectForm(o) {
    const b = L.bbox(o.outline), rooms = Object.fromEntries(floor().rooms.map((r) => [r.id, r.name]));
    let h = `<h3>Object ${editable() ? '<button data-act="del">Delete object</button>' : ''}</h3>` +
      field('Name', 'name', o.name, { data: 'list="mapPresets"', hint: editable() ? 'Pick a common thing to fill in what it is made of and its height.' : '' }) +
      `<datalist id="mapPresets">${Object.keys(L.PRESETS).map((n) => `<option value="${esc(n)}">`).join('')}</datalist>` +
      field('In room', 'room', o.room, { opts: rooms }) +
      field('Made of', 'material', o.material, { opts: MATERIALS }) + field('Built as', 'construction', o.construction, { opts: CONSTRUCTIONS }) +
      field('Off the floor', 'z_min', fmt(o.z_min || 0), { t: 'off' }) + field('Height', 'height', fmt(o.height), { t: 'len' });
    if (L.isRect(o.outline)) h += field('Width', 'w', fmt(b.x1 - b.x0), { t: 'len' }) + field('Depth', 'd', fmt(b.y1 - b.y0), { t: 'len' });
    if (o.note) h += `<p class="muted">${esc(o.note)}</p>`;
    return h;
  }
  function apertureForm(a) {
    const s = L.apertureSpan(doc, fi, a), [dSill, dHead] = AP_DEFAULTS[a.kind] || [0, null];
    let h = `<h3>${esc(AP_KINDS[a.kind] || a.kind)} ${editable() ? '<button data-act="del">Delete</button>' : ''}</h3>` +
      field('Kind', 'kind', a.kind, { opts: AP_KINDS });
    if (s) h += field('Width', 'apw', fmt(s.width), { t: 'len' }) +
      field('From the corner', 'apo', fmt(s.offset), { t: 'off', hint: `Along a ${fmt(s.wall)} wall.` });
    else h += '<p class="bad">Not on a wall: move it onto a room edge.</p>';
    h += field('Sill', 'sill', lenOrBlank(a.sill), { t: 'off', ph: fmt(dSill), data: 'data-opt="1"' }) +
      field('Top', 'head', lenOrBlank(a.head), { t: 'off', ph: dHead == null ? 'the ceiling' : fmt(dHead), data: 'data-opt="1"' });
    if (a.kind === 'door') h += field('Made of', 'material', a.material || 'wood', { opts: DOOR_MATERIALS });
    if (a.note) h += `<p class="muted">${esc(a.note)}</p>`;
    return h;
  }
  function openForm(e) {
    const names = (e.rooms || []).map((id) => floor().rooms.find((r) => r.id === id)?.name || id);
    return `<h3>Open wall ${editable() ? '<button data-act="del">Put the wall back</button>' : ''}</h3>` +
      `<p>No wall between ${esc(names.join(' and '))} for ${fmt(Math.hypot(e.b[0] - e.a[0], e.b[1] - e.a[1]))}.</p>`;
  }
  function floorForm() {
    const f = floor(), n = (f.objects || []).length;
    const moves = editable() && doc.floors.length > 1 ? `<div class="ctl">${fi < doc.floors.length - 1 ? '<button data-act="floorup">Move floor up</button>' : ''}${fi > 0 ? '<button data-act="floordown">Move floor down</button>' : ''}</div>` : '';
    let h = `<h3>Floor ${editable() && doc.floors.length > 1 ? '<button data-act="delfloor">Delete floor</button>' : ''}</h3>` + moves +
      field('Name', 'name', f.name, { data: 'list="mapHaFloors"', hint: haHint('floor', f) }) + haList('mapHaFloors', places.floors) +
      field('Floor level', 'elevation', (f.elevation < 0 ? '-' : '') + fmt(Math.abs(f.elevation)), { t: 'soff', hint: 'Height above the lowest floor.' }) +
      field('Ceiling height', 'ceiling', fmt(f.ceiling), { t: 'len' }) +
      field('Floor thickness', 'slab', fmt(f.slab), { t: 'len', hint: 'From this ceiling to the floor above.' }) +
      `<p class="muted">${f.rooms.length} rooms, ${n} objects, ${(f.apertures || []).length} doors and windows.</p>`;
    return h + receiversHtml(f) + underlayHtml(f);
  }
  // A name either links to Home Assistant (an area, a floor) or is the map's own (Nick 2026-09-30).
  const haList = (id, list) => `<datalist id="${id}">${list.map((x) => `<option value="${esc(x.name)}">`).join('')}</datalist>`;
  function areasFor() {
    const hf = floor().ha_floor;
    return [...places.areas].sort((a, b) => (b.floor === hf) - (a.floor === hf) || a.name.localeCompare(b.name));
  }
  function haHint(kind, it) {
    const [list, key, what] = kind === 'room' ? [places.areas, 'ha_area', 'area'] : [places.floors, 'ha_floor', 'floor'];
    if (it[key]) return `Linked to the Home Assistant ${what} ${esc(list.find((x) => x.id === it[key])?.name || it[key])}.`;
    if (!list.length) return `Home Assistant's ${what}s show here once the bridge reports them.`;
    return `A ${kind} of the map only. Pick a Home Assistant ${what} from the list to link it.`;
  }
  function underlayHtml(f) {
    if (!editable()) return '';
    const u = f.underlay, sc = imageTool.scale;
    let h = '<h3>Image under the plan</h3>';
    if (!u) {
      return h + `<p class="muted">A plan to trace over: a scan, a photo of a sketch, a robot vacuum's map. Not needed - rooms can be
        drawn and dimensioned by hand.</p><div class="ctl"><button data-act="ulload">Load image</button></div>`;
    }
    if (sc) {
      h += sc.pts.length < 2 ? `<p>Click ${sc.pts.length ? 'the second' : 'the first of two'} points on the image whose distance apart you know.</p>`
        : `<p>They are ${fmt(dist(sc.pts[0], sc.pts[1]))} apart on the image.</p>` + field('Really', 'ulreal', '', { t: 'len', ph: `e.g. 12' 6"` });
      return h + `<div class="ctl"><button data-act="ulcancel">Cancel</button></div>`;
    }
    return h + `<div class="ctl"><button data-act="ulscale">Set scale</button><button data-act="ulrot">Rotate 90°</button>
      <button data-act="ulload">Replace</button><button data-act="ulremove">Remove</button></div>
      <div class="mrow"><label>Opacity</label><input type="range" min="0" max="1" step="0.05" data-k="ulop" value="${u.opacity ?? 0.5}"></div>
      <div class="mrow"><label>Locked</label><div><input type="checkbox" data-k="ullock"${u.locked ? ' checked' : ''}>
      <span class="muted mhint">${u.locked ? 'Unlock to move it.' : 'Move it with the Image tool (I).'}</span></div></div>`;
  }
  function problemsHtml() {
    if (!problems.length) return '<h3>Problems</h3><p class="muted">None: the draft builds.</p>';
    const order = problems.map((p, i) => [p, i]).sort((a, b) => (a[0].severity === 'error' ? 0 : 1) - (b[0].severity === 'error' ? 0 : 1));
    return `<h3>Problems</h3>` + order.map(([p, i]) => {
      const fl = p.floor != null && floorIndex(p.floor) >= 0 ? doc.floors[floorIndex(p.floor)].name + ': ' : '';
      return `<div class="mprob ${p.severity}" data-p="${i}">${p.severity === 'error' ? 'Fix' : 'Check'} - ${esc(fl + p.message)}</div>`;
    }).join('');
  }
  let armed = null;
  function draftHtml() {
    if (!hasDraft || !editable()) return '';
    return `<h3>Draft</h3><p class="muted">Changes are saved as a draft. The tracker keeps using the live house until
      you press Apply.</p><div class="ctl"><button data-act="discard" class="${armed === 'discard' ? 'armed' : ''}">${armed === 'discard' ? 'Discard every change?' : 'Discard draft'}</button></div>`;
  }
  const when = (t) => new Date(t * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  function historyHtml() {
    if (!editable()) return '';
    let h = lastApplied ? `<h3>History</h3><p class="muted">The live house was applied ${esc(when(lastApplied))}.</p>` : '';
    if (!versions.length) return h;
    if (!h) h = '<h3>History</h3>';
    return h + '<p class="muted">Houses that earlier applies replaced, newest first.</p>' + versions.map((v) => {
      const key = 'restore:' + v.name;
      return `<div class="mhist"><span>${esc(when(v.at))} - ${v.rooms} rooms on ${v.floors} floors</span>
        <button data-act="restore" data-name="${esc(v.name)}" class="${armed === key ? 'armed' : ''}">${armed === key ? 'Replace the draft?' : 'Open as draft'}</button></div>`;
    }).join('');
  }
  const RUNNING = new Set(['checking', 'build', 'geometry', 'history', 'switching', 'restarting']);
  const MARK = { done: '✓', running: '…', waiting: '·', failed: '✗', skipped: '–' };
  function applyHtml() {
    const st = applying;
    let h = '<h3>Applying the draft</h3><ol class="mapply">' + (st.steps || []).map((s) =>
      `<li class="${s.state}"><span>${MARK[s.state] || ''}</span> ${esc(s.label)}${s.state === 'skipped'
        ? ' <small class="muted">- not yet: no receiver is placed</small>' : ''}</li>`).join('') + '</ol>';
    if (st.state === 'failed') {
      return h + `<p class="bad">Not applied: ${esc(st.error || 'unknown error')}</p><p class="muted">The old house is still live and
        your draft is unchanged.</p><div class="ctl"><button data-act="applyok">Back to editing</button></div>`;
    }
    if (st.state === 'done') return h + '<p>Applied. Reloading...</p>';
    return h + `<p class="muted">${st.down ? 'The engine is restarting on the new house...' : copy.wait}</p>`;
  }
  async function loadHistory() {
    try { versions = (await api('GET', 'api/house/history')).versions || []; } catch (e) { versions = []; }
    if (!sel && !applying) renderPanel();
  }
  async function startApply() {
    try {
      await saver.flush();                                           // what is applied is what you see
      if (saver.state.dirty) return toast('The draft is not saved yet: try again in a moment');
      const r = await fetch('api/house/apply', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const j = await r.json().catch(() => ({}));
      if (r.status === 400) { toast(j.error || 'Cannot apply'); loadProblems(); return; }
      if (!r.ok && r.status !== 409) return toast('Could not apply: ' + (j.error || 'HTTP ' + r.status));
      applying = r.status === 409 ? j.status : j;
      sel = null; everything(); pollApply();
    } catch (e) { toast('Could not apply: ' + e.message); }
  }
  // Every second while it runs; through the restart (the server is away) every 2 s until the new engine says done.
  async function pollApply() {
    clearTimeout(applyT);
    if (!applying) return;
    let st = null;
    try { st = await api('GET', 'api/house/apply'); } catch (e) { st = null; }
    if (!applying) return;
    if (!st) {
      applying = { ...applying, state: 'restarting', down: true,
                   steps: (applying.steps || []).map((s) => ({ ...s, state: s.id === 'restarting' ? 'running' : 'done' })) };
      renderPanel();
      applyT = setTimeout(pollApply, 2000);
      return;
    }
    if (st.state === 'done' && st.hash === applying.hash) {
      applying = st; renderPanel(); toast('Applied');
      try { localStorage.setItem('openMap', '1'); } catch (e) { /* private window */ }
      setTimeout(() => location.reload(), 1500);
      return;
    }
    applying = { ...st, down: false };
    renderPanel(); renderTop();
    if (st.state !== 'failed') applyT = setTimeout(pollApply, st.state === 'restarting' ? 2000 : 1000);
  }
  function renderPanel() {
    if (!active || !doc) return;
    const it = sel ? L.itemOf(doc, sel) : null;
    if (sel && !it) sel = null;
    if (applying) { panel.innerHTML = applyHtml(); return; }
    let h = editable() ? '' : '<p class="muted">Editing needs a wider screen.</p>';
    if (it && sel.kind === 'room') h += roomForm(it);
    else if (it && sel.kind === 'object') h += objectForm(it);
    else if (it && sel.kind === 'aperture') h += apertureForm(it);
    else if (it && sel.kind === 'open') h += openForm(it);
    else if (it && sel.kind === 'receiver') h += receiverForm(it);
    else h += floorForm() + problemsHtml() + draftHtml() + historyHtml();
    const a = document.activeElement, keep = a && panel.contains(a) && a.dataset.k
      ? `[data-k="${a.dataset.k}"]${a.dataset.i ? `[data-i="${a.dataset.i}"]` : ''}` : null;
    panel.innerHTML = h;
    if (keep) panel.querySelector(keep)?.focus();
    preview?.select(roomRef());
  }
  function selectProblem(p) {
    const pf = p.floor != null ? floorIndex(p.floor) : -1;
    for (const id of p.ids || []) {
      const rx = (doc.receivers || []).find((r) => r.id === id);
      if (rx) { fi = floorIndex(rx.floor); sel = L.refOf('receiver', fi, id); break; }
      const st = (doc.stairs || []).find((s) => s.id === id);
      if (st) { fi = floorIndex(st.lower.floor); sel = L.refOf('room', fi, st.lower.room); break; }
      const fis = pf >= 0 ? [pf] : doc.floors.map((_, i) => i);
      const hit = fis.flatMap((i) => [['room', 'rooms'], ['object', 'objects'], ['aperture', 'apertures']]
        .filter(([, key]) => (doc.floors[i][key] || []).some((x) => x.id === id)).map(([kind]) => L.refOf(kind, i, id)))[0];
      if (hit) { fi = hit.fi; sel = hit; break; }
    }
    if (!sel && pf >= 0) fi = pf;
    local.set('mapFloor', floor().id);
    render(); renderTop(); renderPanel();
  }
  const bad = (el, msg) => { el.classList.add('bad'); toast(msg); };
  // Handled a tick later: when Tab caused the change, focus has moved to the next field by then, and renderPanel
  // keeps it there.
  panel.addEventListener('change', (e) => setTimeout(() => onChange(e.target), 0));
  function onChange(el) {
    const k = el.dataset.k;
    if (!k || !doc || !el.isConnected || k === 'rxname') return;          // rxname is read by its Place button
    if (k === 'ulop' || k === 'ullock') {
      const nd = structuredClone(doc), u = nd.floors[fi].underlay;
      if (k === 'ulop') u.opacity = +el.value; else if (el.checked) u.locked = true; else delete u.locked;
      return commit(nd);
    }
    const t = el.dataset.t, raw = el.value;
    let v = raw;
    if (t === 'len') { v = L.parseLength(raw); if (v == null) return bad(el, `Type a length like 12' 6" or 3.8 m`); }
    if (t === 'off' || t === 'soff') {
      if (raw.trim() === '' && el.dataset.opt) v = undefined;
      else { v = L.parseOffset(raw, { signed: t === 'soff' }); if (v == null) return bad(el, `Type a length like 7" or 0.2 m${t === 'soff' ? ', with a minus for below' : ''}`); }
    }
    if (t === 'text' && !raw.trim()) return bad(el, 'A name cannot be empty');
    const ref = sel && L.itemOf(doc, sel) ? sel : L.refOf('floor', fi, floor().id), it = L.itemOf(doc, ref);
    if (k === 'w' || k === 'd') {
      const b = L.bbox(it.outline);
      return commit(L.updateItem(doc, ref, { outline: L.setRectSize(it.outline, k === 'w' ? v : b.x1 - b.x0, k === 'd' ? v : b.y1 - b.y0) }));
    }
    if (k === 'edge') return commit(L.updateItem(doc, ref, { outline: L.setEdgeLength(it.outline, +el.dataset.i, v) }));
    if (k === 'apw') return commit(L.setApertureSpan(doc, ref, { width: v }));
    if (k === 'apo') return commit(L.setApertureSpan(doc, ref, { offset: v }));
    if (k === 'rxh') return commit(L.moveReceiver(doc, ref.id, floorIndex(it.floor), [it.x, it.y], appliedRx(ref.id), L.isoLocal(), v));
    if (k === 'rxfloor') {
      const nf = floorIndex(v);
      const nd = L.moveReceiver(doc, ref.id, nf, [it.x, it.y], appliedRx(ref.id), L.isoLocal());
      if (nd === doc) return toast('The live house has not loaded yet: try again in a moment');
      fi = nf; sel = L.refOf('receiver', fi, ref.id); commit(nd); fit(); return render();
    }
    if (k === 'rxepochat') {
      const t = new Date(raw);
      if (isNaN(t)) return bad(el, 'Pick a date and time');
      return commit(L.setEpochTime(doc, ref.id, el.dataset.at, L.isoLocal(t), appliedRx(ref.id)));
    }
    if (k === 'rxmoved') {
      const t = new Date(raw);
      if (isNaN(t)) return bad(el, 'Pick a date and time');
      return commit(L.setMoveTime(doc, ref.id, L.isoLocal(t), appliedRx(ref.id)));
    }
    if (k === 'dir') return commit(L.setStairsDir(doc, el.dataset.id, v));
    if (k === 'ulreal') return applyScale(v);
    if (k === 'name' && (ref.kind === 'room' || ref.kind === 'floor')) {
      const [list, key] = ref.kind === 'room' ? [places.areas, 'ha_area'] : [places.floors, 'ha_floor'];
      const name = v.trim(), hit = list.find((x) => x.name.toLowerCase() === name.toLowerCase());
      const nd = structuredClone(doc), x = L.itemOf(nd, ref);
      x.name = hit ? hit.name : name;
      if (hit) x[key] = hit.id; else if (list.length) delete x[key];   // no list yet: keep a link made earlier
      return commit(nd);
    }
    const pre = ref.kind === 'object' && k === 'name' && L.PRESETS[v.trim()];
    if (pre) {
      const height = Math.round(Math.min(pre.height, Math.max(0.01, floor().ceiling - pre.z_min)) * 1e6) / 1e6;
      return commit(L.updateItem(doc, ref, { name: v.trim(), material: pre.material, construction: pre.construction, z_min: pre.z_min, height }));
    }
    if (v === undefined) { const nd = structuredClone(doc); delete L.itemOf(nd, ref)[k]; return commit(nd); }
    commit(L.updateItem(doc, ref, { [k]: typeof v === 'string' ? v.trim() : v }));
  }
  panel.addEventListener('input', (e) => {
    e.target.classList?.remove('bad');
    if (e.target.dataset.k === 'ulop') svg.querySelector('image')?.setAttribute('opacity', e.target.value);
  });
  panel.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-act], .mprob');
    if (!b || !doc) return;
    if (b.classList.contains('mprob')) return selectProblem(problems[+b.dataset.p]);
    const act = b.dataset.act;
    if (act === 'del') return remove();
    if (act === 'ulload') return fileIn.click();
    if (act === 'ulscale') { setTool('image'); imageTool.scale = { pts: [] }; render(); return renderPanel(); }
    if (act === 'ulcancel') { imageTool.scale = null; render(); return renderPanel(); }
    if (act === 'ulrot' || act === 'ulremove') {
      const nd = structuredClone(doc), g = nd.floors[fi];
      if (act === 'ulremove') delete g.underlay; else g.underlay.rotation = ((g.underlay.rotation || 0) + 90) % 360;
      return commit(nd);
    }
    if (act === 'applyok') { applying = null; await load(); return; }
    if (act === 'rxsel') {
      const r = (doc.receivers || []).find((x) => x.id === b.dataset.id);
      if (r) { fi = floorIndex(r.floor); sel = L.refOf('receiver', fi, r.id); fit(); render(); renderTop(); renderPanel(); }
      return;
    }
    if (act === 'rxplace' || act === 'rxnew') {
      const name = act === 'rxnew' ? (panel.querySelector('[data-k=rxname]')?.value || '').trim() : b.dataset.name;
      if (!name) return toast('Type the receiver\'s name as Home Assistant shows it');
      if ((doc.receivers || []).some((r) => r.name === name)) return toast('There is a receiver with that name already');
      toPlace = { name, address: act === 'rxplace' ? b.dataset.addr : null };
      setTool('receiver');
      return;
    }
    if (act === 'rxheight') {
      const r = L.itemOf(doc, sel);
      return commit(L.moveReceiver(doc, r.id, floorIndex(r.floor), [r.x, r.y], appliedRx(r.id), L.isoLocal(), +b.dataset.h));
    }
    if (act === 'rxepoch') return commit(L.addEpoch(doc, sel.id, L.isoLocal()));
    if (act === 'rxback') return commit(L.putBack(doc, sel.id, appliedRx(sel.id)));
    if (act === 'rxdelepoch') return commit(L.removeEpoch(doc, sel.id, b.dataset.at, appliedRx(sel.id)));
    if (act === 'restore') {
      const key = 'restore:' + b.dataset.name;
      if (hasDraft && armed !== key) {
        armed = key; renderPanel();
        setTimeout(() => { if (armed === key) { armed = null; renderPanel(); } }, 4000);
        return;
      }
      armed = null;
      try {
        await saver.reset();
        const st = await api('POST', 'api/house/restore', { name: b.dataset.name });
        doc = L.ensureIds(st.doc); hasDraft = st.draft; problems = st.problems || [];
        hist = L.history(200); sel = null; fit(); everything(); schedulePreview();
        toast('Opened as the draft');
      } catch (err) { toast('Could not open it: ' + err.message); }
      return;
    }
    if (act === 'floorup' || act === 'floordown') {
      const dir = act === 'floorup' ? 1 : -1, nd = L.moveFloor(doc, fi, dir);
      if (nd !== doc) { fi += dir; local.set('mapFloor', nd.floors[fi].id); commit(nd); }
      return;
    }
    if (act === 'delfloor' || act === 'discard') {
      if (armed !== act) { armed = act; renderPanel(); setTimeout(() => { if (armed === act) { armed = null; renderPanel(); } }, 4000); return; }
      armed = null;
      if (act === 'delfloor') {
        if (L.receiversOnFloor(doc, floor().id).some((r) => appliedRx(r.id) !== null)) {
          return toast('Receivers in the live house stand on this floor: move them to another floor first (their panel)');
        }
        const nd = L.deleteItem(doc, L.refOf('floor', fi, floor().id)); sel = null; fi = Math.max(0, fi - 1); commit(nd); fit(); return render();
      }
      try { await saver.reset(); await api('DELETE', 'api/house/draft'); await load(); toast('Draft discarded'); }
      catch (err) { toast('Could not discard: ' + err.message); }
    }
  });
  // an armed button (discard, delete floor) disarms when the plan is touched
  root.addEventListener('pointerdown', () => { if (armed) { armed = null; renderPanel(); } });

  return {
    // Another panel is about to write the draft (the placement advisor): the editor's own edits are saved first ...
    async settle() { if (doc) await saver.flush(); return !saver.state.dirty; },   // false: not saved
    // ... and it opens the draft that panel wrote. A hidden editor (or one not opened yet) loads it when it is shown:
    // loading while hidden would fit the plan to a canvas of no size.
    async reload() {
      if (loading) await loading;
      if (!active) { doc = null; return; }
      if (doc) await load();
    },
    setActive(on) {
      active = on;
      root.style.display = on ? '' : 'none'; panel.style.display = on ? '' : 'none';
      preview?.show(on && previewOn);
      if (!on) { if (saver.state.dirty) saver.flush(); return; }
      if (doc) { loadPlaces(); loadReceiverInfo(); }
      if (!doc && !loading) {
        loading = load().catch((e) => { panel.innerHTML = `<p class="bad">Could not open the map: ${esc(e.message)}</p>`; })
          .finally(() => { loading = null; });
      } else if (doc) { render(); renderPanel(); }
    },
  };
}
