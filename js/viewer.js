import * as THREE from 'three';
import * as GaussianSplats3D from 'gaussian-splats-3d';

// Query: ?scene=<id from scenes.json>, or ?model=<ply url>&config=<json url>&title=...
// Extra: &thumb=1 renders a clean frame and (dev server only) saves it as the thumbnail.
const params = new URLSearchParams(location.search);
const thumbMode = params.get('thumb') === '1';
const $ = (id) => document.getElementById(id);

async function resolveScene() {
  const id = params.get('scene');
  if (id) {
    const manifest = await (await fetch('scenes.json')).json();
    const scene = manifest.scenes.find((s) => s.id === id);
    if (!scene) throw new Error(`Unknown scene "${id}"`);
    return scene;
  }
  if (params.get('model')) {
    return { id: 'custom', title: params.get('title') || 'Custom model', subtitle: '', model: params.get('model'), config: params.get('config') };
  }
  location.replace('./');
  throw new Error('No scene selected');
}

function showError(msg) {
  $('loader').hidden = true;
  $('error').hidden = false;
  $('error').textContent = msg;
}

const scene = await resolveScene().catch((e) => { showError(String(e.message || e)); throw e; });
document.title = `${scene.title} · Splat Viewer`;
$('title').textContent = scene.title;
$('subtitle').textContent = scene.subtitle || '';
if (thumbMode) document.body.classList.add('thumb');

let cfg = null;
if (scene.config) {
  try { cfg = await (await fetch(scene.config)).json(); } catch (e) { console.warn('No scene config', e); }
}
if (cfg) $('stats').textContent = `${(cfg.num_splats / 1e6).toFixed(2)}M splats`;

// --- Camera ring guide (torus fitted around the capture cameras) ---------------------------
const threeScene = new THREE.Scene();
let ring = null;
const center = cfg ? new THREE.Vector3().fromArray(cfg.orbit_center) : new THREE.Vector3();
const upVec = cfg ? new THREE.Vector3().fromArray(cfg.camera_up).normalize() : new THREE.Vector3(0, 1, 0);
if (cfg) {
  const t = cfg.torus_config;
  ring = new THREE.Mesh(
    new THREE.TorusGeometry(t.torus_inner_radius, t.torus_ring_radius * 0.05, 8, 160),
    new THREE.MeshBasicMaterial({ color: 0x5cc8ff, transparent: true, opacity: 0.55 }),
  );
  ring.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), upVec); // torus axis -> scene up
  ring.position.fromArray(t.camera_center);
  ring.visible = false;
  threeScene.add(ring);
}

const viewer = new GaussianSplats3D.Viewer({
  cameraUp: upVec.toArray(),
  initialCameraPosition: cfg ? cfg.initial_camera_position : [0, 0, -5],
  initialCameraLookAt: center.toArray(),
  threeScene,
  sharedMemoryForWorkers: false,
  sphericalHarmonicsDegree: 0,
});

// --- Orbit lock: keep the camera inside the range the capture covered -----------------------
let locked = false;
function setOrbitLock(on) {
  const c = viewer.controls;
  if (!c || !cfg) return;
  locked = on;
  const d = cfg.camera_distance;
  c.minDistance = on ? d.min * 0.5 : 0;
  c.maxDistance = on ? d.max * 1.5 : Infinity;
  c.minPolarAngle = on ? Math.PI * 0.25 : 0.1;
  c.maxPolarAngle = on ? Math.PI * 0.6 : Math.PI * 0.75;
  c.enablePan = !on;
  if (on) c.target.copy(center);
  c.update();
  $('lock').classList.toggle('on', on);
}

// --- Camera tour: closed spline through the smoothed capture path, looking at the subject ----
let curve = null, touring = false, tourT = 0, lastTs = 0, recorder = null;
if (cfg?.camera_path?.keyframes?.length) {
  curve = new THREE.CatmullRomCurve3(cfg.camera_path.keyframes.map((p) => new THREE.Vector3().fromArray(p)),
    cfg.camera_path.closed, 'centripetal');
}
const tmp = new THREE.Vector3();

function setTouring(on) {
  if (!curve) return;
  touring = on;
  lastTs = 0;
  $('play').textContent = on ? '❚❚ Pause' : '▶ Tour';
  if (viewer.controls) {
    viewer.controls.enabled = !on;
    if (!on) { viewer.controls.target.copy(center); viewer.controls.update(); }
  }
}

function placeOnTour(u) {
  curve.getPointAt(u, tmp);
  tmp.addScaledVector(upVec, tmp.distanceTo(center) * Number($('lift').value) / 100);
  viewer.camera.up.copy(upVec);
  viewer.camera.position.copy(tmp);
  viewer.camera.lookAt(center);
  if (viewer.controls) viewer.controls.target.copy(center);
}

function tourFrame(ts) {
  requestAnimationFrame(tourFrame);
  if (!touring || !viewer.camera) return;
  const dt = lastTs ? Math.min((ts - lastTs) / 1000, 0.1) : 0;
  lastTs = ts;
  tourT += dt / Number($('speed').value);
  if (recorder && tourT >= 1) stopRecording();
  // Closed paths loop; open (partial-orbit) paths ping-pong back and forth.
  const u = cfg.camera_path.closed ? tourT % 1 : 0.5 - 0.5 * Math.cos(Math.PI * tourT);
  placeOnTour(u);
}
requestAnimationFrame(tourFrame);

// --- Recording: one loop of the tour from the canvas, saved as .webm -------------------------
function startRecording() {
  const canvas = viewer.renderer.domElement;
  const mime = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'].find((m) => MediaRecorder.isTypeSupported(m));
  const chunks = [];
  recorder = new MediaRecorder(canvas.captureStream(60), { mimeType: mime, videoBitsPerSecond: 25_000_000 });
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  recorder.onstop = () => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob(chunks, { type: 'video/webm' }));
    a.download = `${scene.id}-tour.webm`;
    a.click();
  };
  tourT = 0;
  document.body.classList.add('recording');
  $('rec').classList.add('on');
  $('rec').textContent = '■ Stop';
  recorder.start();
  setTouring(true);
}

function stopRecording() {
  if (!recorder) return;
  recorder.stop();
  recorder = null;
  document.body.classList.remove('recording');
  $('rec').classList.remove('on');
  $('rec').textContent = '● Record';
  setTouring(false);
}

// --- UI wiring -------------------------------------------------------------------------------
$('play').onclick = () => setTouring(!touring);
$('rec').onclick = () => (recorder ? stopRecording() : startRecording());
$('lock').onclick = () => setOrbitLock(!locked);
$('ring').onclick = () => { if (ring) { ring.visible = !ring.visible; $('ring').classList.toggle('on', ring.visible); } };
$('full').onclick = () => (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen());
$('speed').oninput = () => { $('speedv').textContent = $('speed').value + 's'; };
window.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === ' ') { e.preventDefault(); setTouring(!touring); }
  else if (e.key === 'r' || e.key === 'R') $('ring').click();
  else if (e.key === 'l' || e.key === 'L') $('lock').click();
  else if (e.key === 'f' || e.key === 'F') $('full').click();
});
// Any manual drag ends the automatic tour.
window.addEventListener('pointerdown', (e) => {
  if (touring && !recorder && e.target.tagName === 'CANVAS') setTouring(false);
});

// --- Load ------------------------------------------------------------------------------------
viewer.start();
try {
  await viewer.addSplatScene(scene.model, {
    showLoadingUI: false,
    progressiveLoad: !thumbMode,
    splatAlphaRemovalThreshold: 5,
    onProgress: (pct, label) => {
      $('bar-fill').style.width = `${Math.max(0, Math.min(100, pct))}%`;
      $('loader-label').textContent = label ? `Loading scene · ${label}` : `Loading scene · ${Math.round(pct)}%`;
    },
  });
} catch (e) {
  console.error(e);
  showError(`Could not load ${scene.model}. Did you add the model file? (${e.message || e})`);
  throw e;
}
$('loader').classList.add('done');
setOrbitLock(!!cfg);
$('dock').hidden = !curve && !cfg;
if (!curve) { $('play').hidden = true; $('rec').hidden = true; }

if (thumbMode) {
  await saveThumbnail();
} else if (curve && params.get('autoplay') !== '0') {
  // Attract mode: start the tour shortly after load; dragging stops it.
  setTimeout(() => { if (!touring && !recorder) setTouring(true); }, 1500);
}

async function saveThumbnail() {
  // Let progressive sorting settle, then grab a frame right after rendering it.
  placeOnTour(0.1);
  await new Promise((r) => setTimeout(r, 2500));
  viewer.update();
  viewer.render();
  const jpg = viewer.renderer.domElement.toDataURL('image/jpeg', 0.85);
  window.__thumb = jpg;
  await fetch(`__thumb/${encodeURIComponent(scene.id)}`, { method: 'POST', body: jpg }).catch(() => {});
  document.body.dataset.thumbDone = '1';
}
