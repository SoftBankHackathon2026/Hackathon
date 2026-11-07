'use strict';
// 시연용 3환경 동시 보기 뷰어. 외부 의존성 없음 (Node 22 내장 모듈만).
//   node demo/viewer/server.js                      http://127.0.0.1:5050
//   node demo/viewer/server.js --config config.local.json
//   PORT=6060 VIEWER_CONFIG=./my.json node demo/viewer/server.js
// config.demo.json(기본값)은 실제 주소, config.local.json 은 리허설용 로컬 docker 다.
// 시연은 환경마다 DB 가 다르다(온프렘 Postgres → AWS RDS CDC). 같은 DB 를 보면
// 칸마다 수치가 같아 전환이 숫자로 드러나지 않는다.
// 설정 파일은 요청마다 다시 읽는다 → 현장에서 주소를 고치면 재시작 없이 반영된다
// (브라우저는 새로고침 한 번). 앱에 CORS 를 추가하지 않으려고 서버가 대신 요청한다.
const fs = require('node:fs');
const http = require('node:http');
const https = require('node:https');
const path = require('node:path');

const PUBLIC_DIR = path.join(__dirname, 'public');
const DEFAULT_CONFIG_FILE = path.join(__dirname, 'config.demo.json');
const DEFAULT_POLL_MILLIS = 500;
// 앱은 DB 를 최대 2초 기다린 뒤 503 으로 답한다. 이보다 짧으면 DB 장애가
// "응답 없음"으로 잘못 보인다.
const DEFAULT_TIMEOUT_MILLIS = 3000;
const MAX_BODY_BYTES = 256 * 1024;
const STATIC_FILES = {
  '/': ['index.html', 'text/html; charset=utf-8'],
  '/viewer.js': ['viewer.js', 'text/javascript; charset=utf-8'],
  '/live-model.js': ['live-model.js', 'text/javascript; charset=utf-8'],
  '/vendor/qrcode.js': ['vendor/qrcode.js', 'text/javascript; charset=utf-8'],
};

function pageUrlOf(address) {
  if (typeof address !== 'string' || address.trim() === '') return null;
  try {
    const url = new URL(address.trim());
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
    return `${url.origin}${url.pathname}`.replace(/\/+$/, '');
  } catch {
    return null;
  }
}

/** --config 의 상대 경로는 어디서 실행하든 이 폴더를 기준으로 한다. */
function configFileFrom(args, env) {
  const flag = args.indexOf('--config');
  if (flag !== -1) return path.resolve(__dirname, args[flag + 1]);
  return env.VIEWER_CONFIG || DEFAULT_CONFIG_FILE;
}

function loadConfig(file) {
  const config = JSON.parse(fs.readFileSync(file, 'utf8'));
  return {
    pollMillis: config.pollMillis || DEFAULT_POLL_MILLIS,
    timeoutMillis: config.timeoutMillis || DEFAULT_TIMEOUT_MILLIS,
    environments: config.environments.map((environment) => ({
      id: environment.id,
      name: environment.name,
      nameJa: environment.nameJa || '',
      pageUrl: pageUrlOf(environment.url),
      // 폰이 여는 주소다. 칸의 숫자와 같은 서버여야 하니 url 과 같게 두고,
      // 로컬 칸은 폰에서 열 수 없으므로 비운다.
      publicUrl: pageUrlOf(environment.public_url),
      localOnly: environment.local_only === true,
      // 주소로 판단하지 않는다. port-forward 로 붙은 실제 서버도 localhost 다.
      localTest: environment.local_test === true,
    })),
  };
}

/**
 * 요청마다 새 연결을 쓴다. keep-alive 로 재사용하면 로드밸런서가 같은 파드로만
 * 보내고, 전환 뒤에도 옛 대상에 붙어 있어 화면이 사실과 달라진다.
 */
function requestLive(pageUrl, timeoutMillis) {
  return new Promise((resolve) => {
    const url = new URL(`${pageUrl}/api/live`);
    const client = url.protocol === 'https:' ? https : http;
    const request = client.request(url, {
      agent: false,
      headers: {'Accept': 'application/json', 'Connection': 'close'},
      timeout: timeoutMillis,
    }, (response) => {
      const chunks = [];
      let size = 0;
      response.on('data', (chunk) => {
        size += chunk.length;
        if (size > MAX_BODY_BYTES) {
          request.destroy();
          resolve({reachable: false, error: 'response too large'});
          return;
        }
        chunks.push(chunk);
      });
      response.on('error', (error) => {
        resolve({reachable: false, error: error.code || 'connection failed'});
      });
      response.on('end', () => {
        let live = null;
        try {
          live = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        } catch { /* 게이트웨이 오류 페이지 등. 상태 코드만 전달한다. */ }
        resolve({reachable: true, status: response.statusCode, live});
      });
    });
    request.on('timeout', () => {
      request.destroy();
      resolve({reachable: false, error: 'timeout'});
    });
    request.on('error', (error) => {
      resolve({reachable: false, error: error.code || 'connection failed'});
    });
    request.end();
  });
}

function sendJson(response, status, body) {
  response.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
  });
  response.end(JSON.stringify(body));
}

function sendStatic(response, [name, contentType]) {
  fs.readFile(path.join(PUBLIC_DIR, name), (error, content) => {
    if (error) return sendJson(response, 500, {error: 'file unavailable'});
    response.writeHead(200, {
      'Content-Type': contentType,
      'Cache-Control': 'no-store',
    });
    response.end(content);
  });
}

/** 대상은 설정 파일의 id 로만 고른다. 브라우저는 주소를 넘길 수 없다. */
async function sendLive(response, id, configFile) {
  let config;
  try {
    config = loadConfig(configFile);
  } catch {
    return sendJson(response, 500, {error: 'config unreadable'});
  }
  const environment = config.environments.find((each) => each.id === id);
  if (!environment) return sendJson(response, 404, {error: 'unknown id'});
  if (!environment.pageUrl) return sendJson(response, 200, {unset: true});
  sendJson(response, 200,
      await requestLive(environment.pageUrl, config.timeoutMillis));
}

function createViewerServer(configFile = DEFAULT_CONFIG_FILE) {
  return http.createServer(async (request, response) => {
    if (request.method !== 'GET') {
      return sendJson(response, 405, {error: 'GET only'});
    }
    let url;
    try {
      url = new URL(request.url, 'http://viewer');
    } catch {
      return sendJson(response, 400, {error: 'bad request'});
    }
    if (STATIC_FILES[url.pathname]) {
      return sendStatic(response, STATIC_FILES[url.pathname]);
    }
    if (url.pathname === '/api/environments') {
      try {
        return sendJson(response, 200, loadConfig(configFile));
      } catch {
        return sendJson(response, 500, {error: 'config unreadable'});
      }
    }
    if (url.pathname === '/api/live') {
      return sendLive(response, url.searchParams.get('id'), configFile);
    }
    sendJson(response, 404, {error: 'not found'});
  });
}

if (require.main === module) {
  const port = Number(process.env.PORT) || 5050;
  // 뷰어가 설정의 주소로 대신 요청하므로 발표 노트북 안에서만 연다.
  const host = process.env.HOST || '127.0.0.1';
  const configFile = configFileFrom(process.argv.slice(2), process.env);
  // 파일 이름을 잘못 쓰면 빈 화면 대신 시작할 때 바로 실패한다.
  loadConfig(configFile);
  createViewerServer(configFile).listen(port, host, () => {
    console.log(`demo viewer: http://${host}:${port} (${configFile})`);
  });
}

module.exports = {configFileFrom, createViewerServer};
