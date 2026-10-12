// Zero-dependency static dev server for the showcase.
//
//   node server.js [--port 8080] [--save-thumbs] [--mount /prefix/=dir ...] [--results file.json]
//
// --save-thumbs   accept POST /__thumb/<id> from viewer.html?thumb=1 and write thumbs/<id>.jpg
//                 (private/thumbs/<id>.jpg for scenes listed in private/scenes.json)
// --mount         serve an extra directory under a URL prefix (used by bench/)
// --results       accept POST /bench-result and write the body to this file (used by bench/)
//
// If private/scenes.json exists (git-ignored), its scenes are appended to /scenes.json, so
// captures you can't publish still show up locally but never reach the repo or a static host.
const http = require('http');
const fs = require('fs');
const path = require('path');

const args = process.argv.slice(2);
const ROOT = __dirname;
const mounts = {};
let port = Number(process.env.PORT) || 8080;
let saveThumbs = false;
let resultsPath = null;
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--port') port = Number(args[++i]);
  else if (args[i] === '--save-thumbs') saveThumbs = true;
  else if (args[i] === '--results') resultsPath = path.resolve(args[++i]);
  else if (args[i] === '--mount') {
    const [prefix, dir] = args[++i].split('=');
    mounts[prefix] = path.resolve(dir);
  }
}

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.hdr': 'application/octet-stream',
  '.ply': 'application/octet-stream', '.png': 'image/png', '.jpg': 'image/jpeg', '.svg': 'image/svg+xml',
  '.wasm': 'application/wasm', '.webm': 'video/webm', '.md': 'text/markdown',
};

function resolvePath(urlPath) {
  for (const [prefix, dir] of Object.entries(mounts)) {
    if (urlPath.startsWith(prefix)) {
      const p = path.join(dir, urlPath.slice(prefix.length));
      return p.startsWith(dir) ? p : null;
    }
  }
  const p = path.join(ROOT, urlPath.endsWith('/') ? urlPath + 'index.html' : urlPath);
  return p.startsWith(ROOT) ? p : null;
}

const PRIVATE_MANIFEST = path.join(ROOT, 'private', 'scenes.json');

function readJson(p) {
  return JSON.parse(fs.readFileSync(p, 'utf8').replace(/^﻿/, ''));
}

function privateScenes() {
  return fs.existsSync(PRIVATE_MANIFEST) ? readJson(PRIVATE_MANIFEST).scenes : [];
}

function readBody(req, cb) {
  let body = '';
  req.on('data', (c) => { body += c; });
  req.on('end', () => cb(body));
}

http.createServer((req, res) => {
  const urlPath = decodeURIComponent(req.url.split('?')[0]);

  if (req.method === 'POST') {
    const thumb = urlPath.match(/^\/__thumb\/([\w-]+)$/);
    if (thumb && saveThumbs) {
      return readBody(req, (body) => {
        const b64 = body.replace(/^data:image\/jpeg;base64,/, '');
        const dir = privateScenes().some((s) => s.id === thumb[1]) ? 'private/thumbs' : 'thumbs';
        fs.mkdirSync(path.join(ROOT, dir), { recursive: true });
        fs.writeFileSync(path.join(ROOT, dir, `${thumb[1]}.jpg`), Buffer.from(b64, 'base64'));
        console.log(`saved ${dir}/${thumb[1]}.jpg`);
        res.writeHead(204); res.end();
      });
    }
    if (urlPath === '/bench-result' && resultsPath) {
      return readBody(req, (body) => { fs.writeFileSync(resultsPath, body); res.writeHead(204); res.end(); });
    }
    res.writeHead(404); return res.end();
  }

  if (urlPath === '/scenes.json' && fs.existsSync(PRIVATE_MANIFEST)) {
    const manifest = readJson(path.join(ROOT, 'scenes.json'));
    manifest.scenes = manifest.scenes.concat(privateScenes());
    const body = JSON.stringify(manifest);
    res.writeHead(200, { 'Content-Type': MIME['.json'], 'Content-Length': Buffer.byteLength(body), 'Cache-Control': 'no-cache' });
    return res.end(req.method === 'HEAD' ? undefined : body);
  }

  const filePath = resolvePath(urlPath);
  if (!filePath) { res.writeHead(403); return res.end(); }
  fs.stat(filePath, (err, stat) => {
    if (err || !stat.isFile()) { res.writeHead(404); return res.end('Not found'); }
    res.writeHead(200, {
      'Content-Type': MIME[path.extname(filePath).toLowerCase()] || 'application/octet-stream',
      'Content-Length': stat.size,
      'Cache-Control': 'no-cache',
    });
    if (req.method === 'HEAD') return res.end();
    fs.createReadStream(filePath).pipe(res);
  });
}).listen(port, () => console.log(`Showcase at http://localhost:${port}`));
