// The map editor's 3-D preview (spec 2026-09-30 section 4): the server's build of the draft (api/house/draft/preview,
// the /api/house shape) in its own small scene, drawn with the viewer's own wall, furniture and stair meshes - so it
// shows exactly the walls and furniture the model will use. Loaded only when the preview is switched on.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { wallMeshes, fixtureMesh, rampMesh, wellOutline } from './geometry.js';
import { floorColor, houseCentre } from './geometry_logic.js';

export function initPreview(el) {
  el.style.position = 'relative';
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.domElement.style.display = 'block';
  el.appendChild(renderer.domElement);
  const bar = document.createElement('div');
  bar.className = 'mpbar';
  bar.innerHTML = '<span class="mpnote"></span><label>explode <input type="range" min="1" max="3" step="0.1" value="1.6"></label>';
  el.appendChild(bar);
  const note = bar.querySelector('.mpnote');
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0b1017);
  scene.add(new THREE.AmbientLight(0xffffff, 0.75));
  const sun = new THREE.DirectionalLight(0xffffff, 0.8);
  sun.position.set(5, 20, 8); scene.add(sun);
  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 400);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  let payload = null, sel = null, framed = false, running = false, explode = 1.6;
  const groups = [], rooms = [];

  function clear() {
    for (const g of groups.splice(0)) {
      scene.remove(g);
      g.traverse((x) => { x.geometry?.dispose?.(); const m = x.material; (Array.isArray(m) ? m : m ? [m] : []).forEach((mm) => mm.dispose?.()); });
    }
    rooms.length = 0;
  }
  function paint() {
    for (const m of rooms) {
      const on = !!sel && sel.fi === m.userData.fi && sel.id === m.userData.id;
      m.material.color.setHex(on ? 0x3b82f6 : floorColor(m.userData.fi));
      m.material.opacity = on ? 0.6 : m.userData.base;
    }
  }
  function build() {
    clear();
    if (!payload) return;
    if (payload.error) { note.textContent = 'The draft does not build yet: ' + payload.error; return; }
    note.textContent = 'What the model will see';
    payload.floors.forEach((f, fi) => {
      const g = new THREE.Group();
      g.position.y = f.z * explode;
      payload.rooms.filter((r) => r.floor === fi).forEach((r) => {
        const geo = new THREE.ShapeGeometry(new THREE.Shape(r.points.map((p) => new THREE.Vector2(p[0], p[1]))));
        geo.rotateX(-Math.PI / 2);
        const m = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ transparent: true, side: THREE.DoubleSide, depthWrite: false }));
        m.userData = { fi, id: r.id, base: r.outdoor ? 0.08 : 0.2 };
        g.add(m); rooms.push(m);
      });
      for (const im of wallMeshes(THREE, f, payload.voxels[fi], (payload.bands || [])[fi], payload.res)) g.add(im);
      payload.fixtures.filter((x) => x.floor === fi && x.height > 0 && x.rf !== 'none').forEach((x) => g.add(fixtureMesh(THREE, x)));
      (payload.ramps || []).filter((r) => r.lower === fi).forEach((r) => g.add(rampMesh(THREE, r)));
      (payload.ramps || []).filter((r) => r.lower + 1 === fi).forEach((r) => g.add(wellOutline(THREE, r)));
      scene.add(g); groups.push(g);
    });
    paint();
    if (!framed) { frame(); framed = true; }
  }
  function frame() {
    if (!payload || payload.error || !payload.floors.length) return;
    const c = houseCentre(payload), top = Math.max(...payload.floors.map((f) => f.z)) * explode;
    const pts = payload.rooms.flatMap((r) => r.points), xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    // stand back far enough for the house's bounding sphere to fit the pane's narrower field of view
    const R = 0.5 * Math.hypot(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), top + 3);
    const vf = (camera.fov * Math.PI) / 180, hf = 2 * Math.atan(Math.tan(vf / 2) * camera.aspect);
    const d = (1.05 * R) / Math.sin(Math.min(vf, hf) / 2), v = new THREE.Vector3(-0.55, 0.62, 0.56).normalize();
    controls.target.set(c.x, top / 2, -c.y);
    camera.position.set(c.x + v.x * d, top / 2 + v.y * d, -c.y + v.z * d);
    controls.update();
  }
  function resize() {
    const w = el.clientWidth, h = el.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix();
  }
  function loop() {
    if (!running) return;
    controls.update(); renderer.render(scene, camera); requestAnimationFrame(loop);
  }
  bar.querySelector('input').addEventListener('input', (e) => {
    explode = +e.target.value;
    if (payload && !payload.error) groups.forEach((g, fi) => { g.position.y = payload.floors[fi].z * explode; });
  });
  addEventListener('resize', () => { if (running) resize(); });
  return {
    update(p, selRef) { payload = p; sel = selRef || null; build(); },
    select(selRef) { sel = selRef || null; paint(); },
    show(on) { const was = running; running = on; if (on) { resize(); if (!was) loop(); } },
    refit() { frame(); },
  };
}
