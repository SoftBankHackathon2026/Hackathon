'use strict';
// 데모 트래픽 제어 서버. 외부 의존성 없음 (Node 22 내장 모듈만). 설계: DESIGN.md
//   OPERATOR_KEY=... AGENT_KEY=... node demo/traffic-control/server.js   http://127.0.0.1:5060
//   HOST=0.0.0.0 PORT=8080 node server.js                                  (클러스터 배포)
// 키가 없으면 해당 역할의 경로는 닫힌다(503). 상태판(GET /api/state)은 누구나 읽는다.
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const auth = require('./lib/auth');
const {createControl} = require('./lib/control');

const PUBLIC_DIR = path.join(__dirname, 'public');
const MAX_BODY_BYTES = 64 * 1024;
const STATIC_FILES = {
  '/': ['index.html', 'text/html; charset=utf-8'],
  '/app.js': ['app.js', 'text/javascript; charset=utf-8'],
  '/hmac.js': ['hmac.js', 'text/javascript; charset=utf-8'],
};
const SECURITY_HEADERS = {
  'Content-Security-Policy': "default-src 'self'; style-src 'self' 'unsafe-inline'; " +
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'Cache-Control': 'no-store',
};

function send(response, status, body, type = 'application/json; charset=utf-8') {
  response.writeHead(status, {...SECURITY_HEADERS, 'Content-Type': type});
  response.end(typeof body === 'string' || Buffer.isBuffer(body) ? body : JSON.stringify(body));
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    request.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(Object.assign(new Error('too large'), {status: 413}));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.on('end', () => resolve(Buffer.concat(chunks)));
    request.on('error', reject);
  });
}

function parseJson(buffer) {
  if (buffer.length === 0) return {};
  try {
    return JSON.parse(buffer.toString('utf8'));
  } catch {
    return undefined;
  }
}

function createServer({keys, control = createControl(), nowSeconds} = {}) {
  const nonces = auth.createNonceCache(nowSeconds);
  const staticCache = new Map();

  function staticFile(name) {
    if (!staticCache.has(name)) {
      staticCache.set(name, fs.readFileSync(path.join(PUBLIC_DIR, name)));
    }
    return staticCache.get(name);
  }

  function check(request, pathname, body, role) {
    return auth.verify({value: request.headers[auth.HEADER], method: request.method,
      path: pathname, body, keys, expectedRole: role, nonces,
      ...(nowSeconds ? {nowSeconds: nowSeconds()} : {})});
  }

  return http.createServer(async (request, response) => {
    let url;
    try {
      url = new URL(request.url, 'http://localhost');
    } catch {
      return send(response, 400, {error: 'BAD_URL'});
    }
    const route = `${request.method} ${url.pathname}`;
    try {
      if (request.method === 'GET' && STATIC_FILES[url.pathname]) {
        const [file, type] = STATIC_FILES[url.pathname];
        return send(response, 200, staticFile(file), type);
      }
      if (route === 'GET /healthz') return send(response, 200, {ok: true});
      if (route === 'GET /api/state') return send(response, 200, control.state());

      const signed = {
        'POST /api/commands': 'operator',
        'GET /agent/commands': 'agent',
        'POST /agent/report': 'agent',
      }[route];
      if (!signed) return send(response, 404, {error: 'NOT_FOUND'});
      if (!keys[signed]) return send(response, 503, {error: 'AUTH_NOT_CONFIGURED'});

      const body = request.method === 'GET' ? Buffer.alloc(0) : await readBody(request);
      // The signature covers the path only; a query string is not part of the contract.
      if (url.search) return send(response, 400, {error: 'QUERY_NOT_ALLOWED'});
      const verdict = check(request, url.pathname, body, signed);
      if (!verdict.ok) return send(response, 401, {error: verdict.code});

      if (route === 'GET /agent/commands') {
        return send(response, 200, {commands: control.take()});
      }
      const payload = parseJson(body);
      if (payload === undefined || payload === null || typeof payload !== 'object') {
        return send(response, 400, {error: 'BAD_JSON'});
      }
      if (route === 'POST /agent/report') {
        const result = control.acceptReport(payload);
        return send(response, result.ok ? 200 : 400, result.ok ? {ok: true} : {error: result.code});
      }
      const result = control.submit({target: payload.target, action: payload.action});
      return send(response, result.status, result.ok ? result.command : {error: result.code});
    } catch (error) {
      return send(response, error.status ?? 500, {error: error.status === 413 ? 'TOO_LARGE' : 'INTERNAL'});
    }
  });
}

function keyFromEnv(name) {
  const value = process.env[name];
  if (value === undefined || value === '') return null;
  if (value.length < 32) throw new Error(`${name} must be at least 32 characters`);
  return value;
}

if (require.main === module) {
  const keys = {operator: keyFromEnv('OPERATOR_KEY'), agent: keyFromEnv('AGENT_KEY')};
  const host = process.env.HOST || '127.0.0.1';
  const port = Number(process.env.PORT || 5060);
  createServer({keys}).listen(port, host, () => {
    process.stdout.write(`demo-traffic-control http://${host}:${port} ` +
      `operator=${keys.operator ? 'on' : 'off'} agent=${keys.agent ? 'on' : 'off'}\n`);
  });
}

module.exports = {createServer};
