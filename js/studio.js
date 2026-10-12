// Splat Studio: reconstructed cars in an HDRI environment, with cast shadows and a blurred planar reflection.
//
// The cars are 3DGS PLYs made by tools/beta_to_3dgs.py: upright (+Y), ground at y = 0, centred, view-dependent
// colour as degree-3 SH. Lighting: the HDRI drives the image-based lighting and a projected sky dome (so the
// car stands on the HDRI's own ground); a directional "sun" casts the splats' shadow onto a shadow catcher,
// and a blurred planar reflector under the car reflects it.
import {
    Application, Asset, AssetListLoader, Color, Entity, EnvLighting, FILLMODE_FILL_WINDOW, LAYERID_DEPTH, Layer,
    RESOLUTION_AUTO, SHADOW_PCF5_16F, SKYTYPE_DOME, TONEMAP_ACES, Vec2, Vec3,
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
const hdri = new Asset('hdri', 'texture', { url: manifest.environment.hdri }, { mipmaps: false });
await new Promise((resolve) => new AssetListLoader([hdri], app.assets).load(resolve));
app.start();

app.scene.skybox = EnvLighting.generateSkyboxCubemap(hdri.resource);
const lighting = EnvLighting.generateLightingSource(hdri.resource);
app.scene.envAtlas = EnvLighting.generateAtlas(lighting);
lighting.destroy();
app.scene.exposure = Number($('exposure').value);
app.scene.gsplat.alphaClip = 0.4;           // splats cast shadows through an alpha-clipped shadow pass
app.scene.sky.type = SKYTYPE_DOME;          // project the HDRI onto a dome with a flat ground
app.scene.sky.node.setLocalScale(new Vec3(60, 60, 60));
app.scene.sky.center = new Vec3(0, 0.03, 0);   // HDRI camera height ~1.8 m above the ground
$('credit').textContent = manifest.environment.credit;

// layers: the reflector is excluded from its own reflection render
const worldLayer = app.scene.layers.getLayerByName('World');
const uiLayer = app.scene.layers.getLayerByName('UI');
const skyLayer = app.scene.layers.getLayerByName('Skybox');
const depthLayer = app.scene.layers.getLayerById(LAYERID_DEPTH);
const reflectorLayer = new Layer({ name: 'Reflector' });
app.scene.layers.insertTransparent(reflectorLayer, app.scene.layers.getTransparentIndex(worldLayer) + 1);

// ---------------------------------------------------------------------------------------- camera
const camera = new Entity('camera');
camera.addComponent('camera', {
    fov: 45, nearClip: 0.05, farClip: 200, toneMapping: TONEMAP_ACES, clearColor: new Color(0, 0, 0),
    layers: [worldLayer.id, depthLayer.id, skyLayer.id, reflectorLayer.id, uiLayer.id],
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
const setSun = () => sun.setEulerAngles(58, Number($('sun').value), 0);
setSun();

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
});
app.root.addChild(reflector);

// ---------------------------------------------------------------------------------------- cars
const cars = new Map();
let current = null;
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
        entity.addComponent('gsplat', { asset, castShadows: true });
        entity.setLocalScale(spec.scale, spec.scale, spec.scale);   // model units -> metres
        cars.set(spec.id, entity);
    }
    if (current) app.root.removeChild(current);
    current = cars.get(spec.id);
    app.root.addChild(current);
    status.textContent = spec.title;
    document.querySelectorAll('#cars button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.id === spec.id)));
    history.replaceState(null, '', `?car=${spec.id}`);
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
const setExposure = () => { app.scene.exposure = Number($('exposure').value); };
for (const ev of ['input', 'change']) {
    $('sun').addEventListener(ev, setSun);
    $('exposure').addEventListener(ev, setExposure);
}

await show(params.get('car') || manifest.cars[0].id).catch((e) => { status.textContent = `Failed to load: ${e}`; });

// handle for debugging from the browser console
window.studio = { app, camera, sun, catcher, reflector, cars };
