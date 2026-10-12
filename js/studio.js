// Splat Studio: reconstructed cars in a photo studio or an HDRI environment, with cast shadows and a blurred
// planar reflection.
//
// The cars are 3DGS PLYs made by tools/beta_to_3dgs.py: upright (+Y), ground at y = 0, centred, view-dependent
// colour as degree-3 SH. Environments (studio.json):
//   cyclorama  a seamless studio backdrop (floor curving into the wall) built as real geometry, lit by a studio
//              HDRI that is used for lighting only; the backdrop receives the car's shadow directly.
//   dome       an HDRI projected onto a dome with a flat ground, so the car stands on the environment's own
//              ground; the shadow lands on a shadow catcher.
//
// Shadows and reflections come from a proxy mesh (tools/splat_to_mesh.py) rather than the splats: splats cast
// shadows through an alpha-clipped pass and leave gaps where they are thin, a mesh is solid. The proxy is drawn
// twice: invisibly into the shadow map, and vertex-coloured on a layer only the reflection camera renders.
import {
    Application, Asset, AssetListLoader, Color, Entity, EnvLighting, FILLMODE_FILL_WINDOW, LAYERID_DEPTH, Layer,
    Mesh, MeshInstance, RESOLUTION_AUTO, SHADOW_PCF5_16F, SKYTYPE_DOME, StandardMaterial, TONEMAP_NEUTRAL,
    Vec2, Vec3,
} from 'playcanvas';
import { BlurredPlanarReflection } from './vendor/blurred-planar-reflection.mjs';
import { CameraControls } from 'playcanvas/scripts/esm/camera-controls.mjs';
import { ShadowCatcher } from 'playcanvas/scripts/esm/shadow-catcher.mjs';

const $ = (id) => document.getElementById(id);
const status = $('status');

const manifest = await (await fetch('studio.json', { cache: 'no-store' })).json();
const params = new URLSearchParams(location.search);

const canvas = $('app');
const app = new Application(canvas, { graphicsDeviceOptions: { antialias: false } });
app.graphicsDevice.maxPixelRatio = Math.min(window.devicePixelRatio, 2);
app.setCanvasFillMode(FILLMODE_FILL_WINDOW);
app.setCanvasResolution(RESOLUTION_AUTO);
window.addEventListener('resize', () => app.resizeCanvas());

// ---------------------------------------------------------------------------------------- environment
app.start();
// The splat colours come from photos that are already tone-mapped. Exposure stays at 1 and the Khronos PBR
// Neutral tone mapper (identity up to ~0.8) keeps them as captured, while still compressing the HDRI's
// highlights. The slider changes the sky's brightness instead of the exposure.
app.scene.exposure = 1;
app.scene.sky.type = SKYTYPE_DOME;          // project the HDRI onto a dome with a flat ground

const envCache = new Map();                 // hdri url -> { skybox, atlas }
let currentEnv = null;

async function loadHdri(url) {
    if (!envCache.has(url)) {
        const hdri = new Asset(url, 'texture', { url }, { mipmaps: false });
        await new Promise((resolve) => new AssetListLoader([hdri], app.assets).load(resolve));
        const lighting = EnvLighting.generateLightingSource(hdri.resource);
        envCache.set(url, { skybox: EnvLighting.generateSkyboxCubemap(hdri.resource),
            atlas: EnvLighting.generateAtlas(lighting) });
        lighting.destroy();
        hdri.unload();
    }
    return envCache.get(url);
}

// Seamless cyclorama: flat floor (radius r0) sweeping up through a quarter-circle cove (radius rc) into a
// vertical wall, revolved around the car. Normals face inwards; the camera stays inside it.
function buildCyclorama(r0 = 13, rc = 6, height = 26, segments = 128) {
    const profile = [[0, 0, 0, 1]];                                   // [r, y, nr, ny]
    for (let i = 0; i <= 16; i++) {
        const phi = (i / 16) * Math.PI / 2;
        profile.push([r0 + rc * Math.sin(phi), rc * (1 - Math.cos(phi)), -Math.sin(phi), Math.cos(phi)]);
    }
    profile.push([r0 + rc, height, -1, 0]);
    const pos = [], nrm = [], idx = [];
    for (let k = 0; k <= segments; k++) {
        const a = (k / segments) * Math.PI * 2, c = Math.cos(a), sn = Math.sin(a);
        for (const [r, y, nr, ny] of profile) {
            pos.push(r * c, y, r * sn);
            nrm.push(nr * c, ny, nr * sn);
        }
    }
    const n = profile.length;
    for (let k = 0; k < segments; k++) {
        for (let j = 0; j < n - 1; j++) {
            const a = k * n + j, b = (k + 1) * n + j;
            idx.push(a, a + 1, b, b, a + 1, b + 1);
        }
    }
    const mesh = new Mesh(app.graphicsDevice);
    mesh.setPositions(pos);
    mesh.setNormals(nrm);
    mesh.setIndices(idx);
    mesh.update();
    return mesh;
}
const cycMaterial = new StandardMaterial();
cycMaterial.gloss = 0.25;
cycMaterial.useMetalness = true;
cycMaterial.metalness = 0;
cycMaterial.update();
const cyclorama = new Entity('cyclorama');
cyclorama.addComponent('render', { meshInstances: [new MeshInstance(buildCyclorama(), cycMaterial)],
    castShadows: false, receiveShadows: false });   // the shadow catcher draws the shadow (as in dome mode)
cyclorama.enabled = false;
app.root.addChild(cyclorama);

function applyBrightness() {
    const v = Number($('exposure').value);
    if (currentEnv?.type === 'cyclorama') {
        const [r, g, b] = currentEnv.color;
        cycMaterial.diffuse = new Color(r * v, g * v, b * v);
        cycMaterial.update();
        camera.camera.clearColor = new Color(r * v, g * v, b * v);
    } else {
        app.scene.skyboxIntensity = v;
    }
}

async function setEnvironment(id) {
    const env = manifest.environments.find((e) => e.id === id) || manifest.environments[0];
    status.textContent = `Loading ${env.title}…`;
    const cyc = env.type === 'cyclorama';
    const { skybox, atlas } = await loadHdri(cyc ? env.lightingHdri : env.hdri);
    app.scene.envAtlas = atlas;                     // image-based lighting in both modes
    app.scene.skybox = skybox;
    currentEnv = env;
    cyclorama.enabled = cyc;
    catcher.enabled = $('shadows').getAttribute('aria-pressed') === 'true';
    const layers = camera.camera.layers.filter((l) => l !== skyLayer.id);
    camera.camera.layers = cyc ? layers : [skyLayer.id, ...layers];   // the backdrop replaces the sky
    if (!cyc) {
        app.scene.sky.node.setLocalScale(new Vec3(env.dome, env.dome, env.dome));   // dome radius in metres
        app.scene.sky.center = new Vec3(0, env.domeCenterY, 0);                      // HDRI camera height / radius
    }
    $('credit').textContent = env.credit;
    document.querySelectorAll('#envs button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.id === env.id)));
    applyBrightness();
    applyLight(true);
    status.textContent = cars.size ? (manifest.cars.find((c) => c.id === currentCar)?.title || '') : status.textContent;
    updateUrl();
}

// layers: the reflector is excluded from its own reflection render
const worldLayer = app.scene.layers.getLayerByName('World');
const uiLayer = app.scene.layers.getLayerByName('UI');
const skyLayer = app.scene.layers.getLayerByName('Skybox');
const depthLayer = app.scene.layers.getLayerById(LAYERID_DEPTH);
const reflectorLayer = new Layer({ name: 'Reflector' });
// before the World transparent pass: the floor reflection must be drawn under the splats (which write no depth)
app.scene.layers.insertTransparent(reflectorLayer, app.scene.layers.getTransparentIndex(worldLayer));
// proxy meshes seen only by the reflection camera
const proxyLayer = new Layer({ name: 'ReflectionProxy' });
app.scene.layers.insertOpaque(proxyLayer, app.scene.layers.getOpaqueIndex(worldLayer) + 1);

// ---------------------------------------------------------------------------------------- camera
const camera = new Entity('camera');
camera.addComponent('camera', {
    fov: 45, nearClip: 0.05, farClip: 200, toneMapping: TONEMAP_NEUTRAL, clearColor: new Color(0, 0, 0),
    layers: [worldLayer.id, depthLayer.id, skyLayer.id, reflectorLayer.id, uiLayer.id,
        ...(params.get('mesh') === '1' ? [proxyLayer.id] : [])],   // ?mesh=1 shows the proxy
});
camera.setLocalPosition(5.5, 1.6, 6.5);
camera.addComponent('script');
app.root.addChild(camera);
camera.camera.requestSceneColorMap(true);
const controls = camera.script.create(CameraControls);
controls.focusPoint = new Vec3(0, 0.75, 0);
controls.enableFly = false;
controls.pitchRange = new Vec2(-80, -2);    // stay above the ground
controls.zoomRange = new Vec2(3, 18);

// ---------------------------------------------------------------------------------------- sun, shadow, floor
const sun = new Entity('sun');
sun.addComponent('light', {
    type: 'directional', color: new Color(1, 0.97, 0.92), intensity: 1.2, castShadows: true,
    shadowType: SHADOW_PCF5_16F, shadowResolution: 2048, shadowDistance: 20, shadowIntensity: 0.75,
    shadowBias: 0.1, normalOffsetBias: 0.05, penumbraSize: 6, penumbraFalloff: 2,
});
app.root.addChild(sun);
// Each environment has its own key light: a low sun outdoors, a steep overhead light in the studios.
// The slider rotates it around the car; switching environment resets it to that environment's default.
function applyLight(reset = false) {
    const l = currentEnv.light;
    if (reset) $('sun').value = l.azimuth;
    sun.light.intensity = l.intensity;
    sun.light.shadowIntensity = l.shadowIntensity ?? 0.75;
    sun.setEulerAngles(90 - l.elevation, Number($('sun').value), 0);   // X rotation tilts the light from straight down
}
const setSun = () => applyLight(false);

const catcher = new Entity('shadow-catcher');
catcher.addComponent('script');
catcher.script.create(ShadowCatcher).scale = new Vec3(16, 16, 16);
app.root.addChild(catcher);

const reflector = new Entity('reflector');
reflector.addComponent('render', { type: 'plane', layers: [reflectorLayer.id], castShadows: false });
reflector.setLocalScale(14, 1, 14);
reflector.setLocalPosition(0, 0.002, 0);
reflector.addComponent('script');
const reflection = reflector.script.create(BlurredPlanarReflection);
Object.assign(reflection, {
    mainCamera: camera, resolution: 0.6, blurAmount: 0.5, intensity: 0.4,
    fadeStrength: 1.2, angleFade: 0.6, heightRange: 1.5, fadeColor: new Color(0, 0, 0, 0),   // transparent clear
    extraLayers: [proxyLayer.id],
});
app.root.addChild(reflector);

// ---------------------------------------------------------------------------------------- cars
const shadowOnly = new StandardMaterial();      // writes depth into shadow maps, nothing on screen
shadowOnly.redWrite = shadowOnly.greenWrite = shadowOnly.blueWrite = shadowOnly.alphaWrite = false;
shadowOnly.depthWrite = false;
shadowOnly.update();
const vertexColour = new StandardMaterial();    // unlit vertex colours for the reflection proxy
vertexColour.useLighting = false;
vertexColour.useSkybox = false;
vertexColour.diffuse = new Color(0, 0, 0);
vertexColour.emissive = new Color(1, 1, 1);
vertexColour.emissiveVertexColor = true;
vertexColour.update();

async function addProxy(carEntity, spec) {
    const glb = new Asset(`${spec.id}-proxy`, 'container', { url: spec.proxy });
    await new Promise((resolve, reject) => {
        glb.once('load', resolve);
        glb.once('error', reject);
        app.assets.add(glb);
        app.assets.load(glb);
    });
    const caster = glb.resource.instantiateRenderEntity({ castShadows: true, receiveShadows: false });
    const mirror = glb.resource.instantiateRenderEntity({ castShadows: false, receiveShadows: false });
    caster.findComponents('render').forEach((r) => { r.meshInstances.forEach((m) => { m.material = shadowOnly; }); });
    mirror.findComponents('render').forEach((r) => {
        r.layers = [proxyLayer.id];
        r.meshInstances.forEach((m) => { m.material = vertexColour; });
    });
    carEntity.addChild(caster);
    carEntity.addChild(mirror);
}

const cars = new Map();
let current = null;
let currentCar = null;
function updateUrl() {
    const q = new URLSearchParams();
    if (currentCar) q.set('car', currentCar);
    if (currentEnv) q.set('env', currentEnv.id);
    if (params.get('mesh') === '1') q.set('mesh', '1');
    history.replaceState(null, '', `?${q}`);
}
async function show(id) {
    const spec = manifest.cars.find((c) => c.id === id) || manifest.cars[0];
    if (!cars.has(spec.id)) {
        status.textContent = `Loading ${spec.title}…`;
        const asset = new Asset(spec.id, 'gsplat', { url: spec.model });
        await new Promise((resolve, reject) => {
            asset.once('load', resolve);
            asset.once('error', reject);
            app.assets.add(asset);
            app.assets.load(asset);
        });
        const entity = new Entity(spec.id);
        entity.addComponent('gsplat', { asset, castShadows: !spec.proxy });
        entity.setLocalScale(spec.scale, spec.scale, spec.scale);   // model units -> metres
        if (spec.proxy) await addProxy(entity, spec);
        cars.set(spec.id, entity);
    }
    if (current) app.root.removeChild(current);
    current = cars.get(spec.id);
    app.root.addChild(current);
    status.textContent = spec.title;
    document.querySelectorAll('#cars button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.id === spec.id)));
    currentCar = spec.id;
    updateUrl();
}

for (const e of manifest.environments) {
    const b = document.createElement('button');
    b.textContent = e.title;
    b.dataset.id = e.id;
    b.onclick = () => setEnvironment(e.id).catch((err) => { status.textContent = `Failed to load: ${err}`; });
    $('envs').appendChild(b);
}

for (const c of manifest.cars) {
    const b = document.createElement('button');
    b.textContent = c.title;
    b.dataset.id = c.id;
    b.onclick = () => show(c.id).catch((e) => { status.textContent = `Failed to load: ${e}`; });
    $('cars').appendChild(b);
}

// ---------------------------------------------------------------------------------------- controls
const toggle = (btn, apply) => {
    btn.onclick = () => {
        const on = btn.getAttribute('aria-pressed') !== 'true';
        btn.setAttribute('aria-pressed', String(on));
        apply(on);
    };
};
toggle($('shadows'), (on) => { sun.light.castShadows = on; catcher.enabled = on; });
toggle($('reflection'), (on) => { reflector.enabled = on; });
const setExposure = applyBrightness;
for (const ev of ['input', 'change']) {
    $('sun').addEventListener(ev, setSun);
    $('exposure').addEventListener(ev, setExposure);
}

await setEnvironment(params.get('env') || manifest.environments[0].id);
await show(params.get('car') || manifest.cars[0].id).catch((e) => { status.textContent = `Failed to load: ${e}`; });

// handle for debugging from the browser console
window.studio = { app, camera, sun, catcher, reflector, cars };
