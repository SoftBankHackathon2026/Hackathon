'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const {createViewerServer} = require('../server');
const {
  classifyPoll, createIngestTracker, createVersionTracker, noResponseDetail,
  splitChartSegments, versionColor,
} = require('../public/live-model');

const listen = (server) => new Promise((resolve) => {
  server.listen(0, '127.0.0.1', () => {
    resolve(`http://127.0.0.1:${server.address().port}`);
  });
});

function closeAfter(t, server) {
  t.after(() => {
    server.closeAllConnections();
    return new Promise((resolve) => server.close(resolve));
  });
}

/** Starts the viewer against a config file written for this test. */
async function startViewer(t, environments, extra = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'viewer-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  const file = path.join(directory, 'config.json');
  fs.writeFileSync(file, JSON.stringify({environments, ...extra}));
  const viewer = createViewerServer(file);
  closeAfter(t, viewer);
  return {base: await listen(viewer), file};
}

async function startApp(t, handler) {
  const app = http.createServer(handler);
  closeAfter(t, app);
  return listen(app);
}

const liveOf = async (base, id) =>
  (await fetch(`${base}/api/live?id=${id}`)).json();

test('the viewer relays an environment answer without changing it',
    async (t) => {
      const live = {environment: 'aws', database: {name: 'db', row_count: '1'}};
      let requested;
      const app = await startApp(t, (request, response) => {
        requested = request;
        response.end(JSON.stringify(live));
      });
      const {base} = await startViewer(t, [{id: 'aws', name: 'AWS', url: app}]);
      assert.deepEqual(await liveOf(base, 'aws'),
          {reachable: true, status: 200, live});
      assert.equal(requested.url, '/api/live');
      assert.equal(requested.headers.connection, 'close');
    });

test('a blank or invalid address is unset and is never requested',
    async (t) => {
      const {base} = await startViewer(t, [
        {id: 'blank', name: 'A', url: ''},
        {id: 'invalid', name: 'B', url: 'not a url'},
        {id: 'scheme', name: 'C', url: 'file:///etc/passwd'},
      ]);
      for (const id of ['blank', 'invalid', 'scheme']) {
        assert.deepEqual(await liveOf(base, id), {unset: true}, id);
      }
    });

test('the browser can only choose a configured environment', async (t) => {
  const {base} = await startViewer(t, []);
  const response = await fetch(
      `${base}/api/live?id=x&url=http://169.254.169.254/`);
  assert.equal(response.status, 404);
});

test('a stopped app is reported as unreachable', async (t) => {
  const stopped = http.createServer();
  const url = await listen(stopped);
  await new Promise((resolve) => stopped.close(resolve));
  const {base} = await startViewer(t, [{id: 'down', name: 'D', url}]);
  const result = await liveOf(base, 'down');
  assert.equal(result.reachable, false);
  assert.equal(classifyPoll(result), 'no-response');
});

test('a hung app times out instead of holding the poll', async (t) => {
  const app = await startApp(t, () => {});
  const {base} = await startViewer(t, [{id: 'hung', name: 'H', url: app}],
      {timeoutMillis: 100});
  assert.deepEqual(await liveOf(base, 'hung'),
      {reachable: false, error: 'timeout'});
});

test('the default timeout outlasts the app waiting two seconds for its DB',
    async (t) => {
      const body = {error: {code: 'DB_UNAVAILABLE'}, environment: 'aws',
        region: 'r', version: 'v'};
      const app = await startApp(t, (request, response) => {
        setTimeout(() => {
          response.statusCode = 503;
          response.end(JSON.stringify(body));
        }, 2100);
      });
      const {base} = await startViewer(t, [{id: 'slow', name: 'S', url: app}]);
      assert.equal(classifyPoll(await liveOf(base, 'slow')), 'db-down');
    });

test('the address change applies without restarting', async (t) => {
  const app = await startApp(t, (request, response) => response.end('{}'));
  const {base, file} = await startViewer(t, [{id: 'a', name: 'A', url: ''}]);
  assert.deepEqual(await liveOf(base, 'a'), {unset: true});
  fs.writeFileSync(file, JSON.stringify({
    environments: [{id: 'a', name: 'A', url: app}],
  }));
  assert.equal((await liveOf(base, 'a')).reachable, true);
});

test('static files are served and nothing else is', async (t) => {
  const {base} = await startViewer(t, []);
  assert.match(await (await fetch(`${base}/`)).text(), /<template id="panel/);
  assert.equal((await fetch(`${base}/viewer.js`)).status, 200);
  assert.equal((await fetch(`${base}/config.json`)).status, 404);
  assert.equal((await fetch(`${base}/..%2fserver.js`)).status, 404);
});

test('environment colors beat the default bar color', () => {
  const page = fs.readFileSync(
      path.join(__dirname, '../public/index.html'), 'utf8');
  // A class on the default rule would outrank [data-environment] and gray out
  // every segment.
  assert.match(page, /^\s*g \{ --environment:/m);
  assert.doesNotMatch(page, /\.\w+ g \{/);
});

test('a malformed request path cannot kill the viewer', async (t) => {
  const {base} = await startViewer(t, []);
  const {hostname, port} = new URL(base);
  await new Promise((resolve) => {
    const socket = net.connect(Number(port), hostname, () => {
      socket.write(
          'GET //[ HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n');
    });
    socket.resume();
    socket.on('close', resolve);
  });
  assert.equal((await fetch(`${base}/`)).status, 200);
});

test('an oversized answer is refused', async (t) => {
  const app = await startApp(t, (request, response) => {
    response.end(Buffer.alloc(300 * 1024, 'x'));
  });
  const {base} = await startViewer(t, [{id: 'big', name: 'B', url: app}]);
  assert.deepEqual(await liveOf(base, 'big'),
      {reachable: false, error: 'response too large'});
});

test('an address with a query still reaches /api/live', async (t) => {
  let requested;
  const app = await startApp(t, (request, response) => {
    requested = request.url;
    response.end('{}');
  });
  const {base} = await startViewer(t,
      [{id: 'q', name: 'Q', url: `${app}/?x=1#y`}]);
  await liveOf(base, 'q');
  assert.equal(requested, '/api/live');
});

test('each poll is classified for the panel', () => {
  const identity = {environment: 'aws', region: 'r', version: 'v'};
  const live = {...identity, database: {name: 'db'}, recent: []};
  const failed = {...identity, error: {code: 'DB_UNAVAILABLE'}, database: null};
  const cases = [
    [{unset: true}, 'unset'],
    [{reachable: false, error: 'timeout'}, 'no-response'],
    [{reachable: true, status: 200, live}, 'live'],
    [{reachable: true, status: 503, live: failed}, 'db-down'],
    [{reachable: true, status: 503, live: null}, 'no-response'],
    [{reachable: true, status: 502, live: {}}, 'no-response'],
    [{reachable: true, status: 404, live: {}}, 'no-response'],
    [{reachable: true, status: 200, live: {database: {}}}, 'no-response'],
    [{reachable: true, status: 200, live: {...identity, database: {}}},
      'no-response'],
    [{reachable: true, status: 503, live: {error: 'gateway'}}, 'no-response'],
  ];
  for (const [result, expected] of cases) {
    assert.equal(classifyPoll(result), expected, JSON.stringify(result));
  }
});

test('an app without /api/live says so', () => {
  assert.match(noResponseDetail({reachable: true, status: 404}), /미배포/);
});

test('bars keep their environment and the peak is the current one only',
    () => {
      const tracker = createIngestTracker();
      const next = (environment, rowCount, seconds) => tracker.record({
        identity: `${environment}/db`, environment, rowCount,
        observedAtMs: seconds * 1000, newestCreatedAtMs: null,
      });
      next('aws', 100, 1000);
      next('aws', 150, 1001);
      next('gcp', 5000, 1004);
      assert.equal(tracker.averageRate(1004, 10), null);
      assert.equal(tracker.peakRate(1004, 60), null);
      next('gcp', 5002, 1005);
      assert.deepEqual(tracker.bars(1004, 5), [
        {rate: 50, environment: 'aws'}, null, null, null,
        {rate: 2, environment: 'gcp'},
      ]);
      assert.equal(tracker.peakRate(1004, 60), 2);
      assert.equal(tracker.averageRate(1004, 10), 2);
    });

/** Just enough DOM for viewer.js: parts by name, text, classes, attributes. */
function fakeElement() {
  const classes = new Set();
  return {
    textContent: '', hidden: false, dataset: {}, style: {}, children: [],
    src: '', href: '', attributes: {},
    classList: {
      toggle(name, on = !classes.has(name)) {
        if (on) classes.add(name);
        else classes.delete(name);
      },
      contains: (name) => classes.has(name),
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
  };
}

const IDENTITY = {environment: 'aws', region: 'r', version: 'a'.repeat(40)};

/** Runs the real viewer.js for one panel against scripted poll answers. */
async function openViewer(answers, environment = {}) {
  const parts = new Map();
  const part = (name) => {
    if (!parts.has(name)) parts.set(name, fakeElement());
    return parts.get(name);
  };
  const root = {
    ...fakeElement(),
    querySelector: (selector) => part(selector.match(/"(.+)"/)[1]),
    cloneNode() { return this; },
  };
  const page = new Map();
  const context = {
    createIngestTracker, createVersionTracker, classifyPoll, noResponseDetail,
    splitChartSegments, versionColor,
    document: {
      getElementById(id) {
        if (id === 'panel-template') {
          return {content: {firstElementChild: root}};
        }
        if (!page.has(id)) page.set(id, fakeElement());
        return page.get(id);
      },
      createElementNS: fakeElement,
      createElement: fakeElement,
      body: fakeElement(),
      addEventListener(type, listener) { context.keydown = listener; },
    },
    fetch: async (url) => {
      if (url === '/api/environments') {
        return {json: async () => ({pollMillis: 500, timeoutMillis: 3000,
          environments: [{id: 'a', name: 'A', nameJa: '', pageUrl: 'http://a',
            publicUrl: null, ...environment}],
        })};
      }
      context.polls++;
      return {json: async () => answers.shift() || {reachable: false}};
    },
    polls: 0,
    qrcode: () => ({
      addData(text) { context.qrText = text; },
      make() {},
      getModuleCount: () => 21,
      isDark: () => true,
    }),
    AbortSignal,
    ResizeObserver: class { observe() {} },
    setTimeout: (callback) => { context.nextPoll = callback; },
    setInterval: (callback) => { context.render = callback; },
  };
  vm.runInNewContext(fs.readFileSync(
      path.join(__dirname, '../public/viewer.js'), 'utf8'), context);
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  await settle();
  await settle();
  return {
    part,
    context,
    async next() {
      context.nextPoll();
      await settle();
      await settle();
    },
  };
}

function liveAnswer(rows, observedOffsetMs, overrides = {}) {
  return {reachable: true, status: 200, live: {
    ...IDENTITY, database: {name: 'db', row_count: String(rows)}, recent: [],
    writes_paused: false,
    observed_at: new Date(Date.now() + observedOffsetMs).toISOString(),
    ...overrides,
  }};
}

test('the panel shows a live answer and loads the real page', async () => {
  const screen = await openViewer(
      [liveAnswer(1000, -1000), liveAnswer(1010, 0)]);
  await screen.next();
  screen.context.render();
  assert.equal(screen.part('status').textContent, '실시간');
  assert.equal(screen.part('total').textContent, '1,010');
  assert.equal(screen.part('rate').textContent, '10.0');
  assert.equal(screen.part('version').children[0].textContent, 'aaaaaaa');
  assert.equal(screen.part('frame').src, 'http://a/');
  assert.equal(screen.part('banner').hidden, true);
});

test('an outage says no response and claims no rate', async () => {
  const screen = await openViewer([liveAnswer(1000, -1000), liveAnswer(1010, 0),
    {reachable: false, error: 'ECONNREFUSED'}]);
  await screen.next();
  screen.context.render();
  assert.equal(screen.part('rate').textContent, '10.0');
  await screen.next();
  screen.context.render();
  assert.match(screen.part('status').textContent, /^응답 없음 \d+초$/);
  assert.equal(screen.part('detail').textContent, 'ECONNREFUSED');
  assert.equal(screen.part('rate').textContent, '—');
  assert.equal(screen.part('total').textContent, '1,010');
});

test('a foreign JSON answer cannot stop polling or fake a live panel',
    async () => {
      const screen = await openViewer([
        {reachable: true, status: 200, live: {database: {}}},
        liveAnswer(1000, 0),
      ]);
      screen.context.render();
      assert.match(screen.part('status').textContent, /^응답 없음/);
      await screen.next();
      screen.context.render();
      assert.equal(screen.part('status').textContent, '실시간');
    });

test('an app without /api/live still shows its real page', async () => {
  const screen = await openViewer([{reachable: true, status: 404, live: null}]);
  screen.context.render();
  assert.match(screen.part('detail').textContent, /미배포/);
  assert.equal(screen.part('frame').src, 'http://a/');
});

test('writes_paused raises the banner; a blank address stays unset',
    async () => {
  const paused = await openViewer([liveAnswer(1, 0, {writes_paused: true})]);
  assert.equal(paused.part('banner').hidden, false);
  const unset = await openViewer([{unset: true}]);
  unset.context.render();
  assert.equal(unset.part('status').textContent, '');
  assert.equal(unset.part('frame').src, '');
});

test('P pauses polling by physical key, so a Korean IME cannot block it',
    async () => {
      const screen = await openViewer([liveAnswer(1, 0)]);
      const polls = screen.context.polls;
      screen.context.keydown({code: 'KeyP', key: 'ㅔ'});
      await screen.next();
      assert.equal(screen.context.polls, polls);
      const tag = screen.context.document.getElementById('paused');
      assert.equal(tag.hidden, false);
      screen.context.keydown({code: 'KeyP', key: 'ㅔ'});
      await screen.next();
      assert.equal(screen.context.polls, polls + 1);
    });

test('the QR holds the public address; without one the panel says local only',
    async () => {
      const screen = await openViewer([], {pageUrl: 'http://proxy-side',
        publicUrl: 'https://app.example.com'});
      assert.equal(screen.context.qrText, 'https://app.example.com/');
      assert.equal(screen.part('local-note').hidden, true);
      const local = await openViewer([]);
      assert.equal(local.context.qrText, undefined);
      assert.equal(local.part('local-note').hidden, false);
      assert.equal(local.part('qr').attributes.hidden, '');
    });

test('the config keeps the public address apart from the polled one',
    async (t) => {
      const {base} = await startViewer(t, [
        {id: 'a', name: 'A', url: 'http://10.0.0.1:8080/',
          public_url: 'https://app.example.com/'},
        {id: 'b', name: 'B', url: 'http://10.0.0.2:8080', public_url: ''},
      ]);
      const {environments} =
        await (await fetch(`${base}/api/environments`)).json();
      assert.equal(environments[0].pageUrl, 'http://10.0.0.1:8080');
      assert.equal(environments[0].publicUrl, 'https://app.example.com');
      assert.equal(environments[1].publicUrl, null);
    });

test('the version has its own row so a canary mix is never cut off', () => {
  const page = fs.readFileSync(
      path.join(__dirname, '../public/index.html'), 'utf8');
  const meta = page.slice(page.indexOf('<div class="meta">'),
      page.indexOf('<div class="version-row">'));
  assert.doesNotMatch(meta, /data-part="version"/);
  // A line rule would also repaint the version marks.
  assert.doesNotMatch(page, /\.chart line \{/);
});

test('the list shows by default and W brings up the real page', async () => {
  const row = {request_id_prefix: 'aaaaaaaa', run_id: '<b>x</b>', phase: 'p',
    created_at: '2026-11-06T00:00:00.000Z'};
  const screen = await openViewer([liveAnswer(1, 0, {recent: [row]})]);
  const [cell] = screen.part('rows').children[0].children;
  assert.equal(cell.textContent, 'aaaaaaaa');
  assert.equal(screen.part('rows').children[0].children[1].textContent,
      '<b>x</b>');
  const {classList} = screen.context.document.body;
  assert.equal(classList.contains('frame-view'), false);
  screen.context.keydown({code: 'KeyW'});
  assert.equal(classList.contains('frame-view'), true);
  screen.context.keydown({code: 'KeyW'});
  assert.equal(classList.contains('frame-view'), false);
});

test('the rate falls to 0.0 at once while the pause banner is up', async () => {
  const screen = await openViewer([liveAnswer(100, -1000), liveAnswer(110, 0),
    liveAnswer(110, 500, {writes_paused: true})]);
  await screen.next();
  screen.context.render();
  assert.equal(screen.part('rate').textContent, '10.0');
  await screen.next();
  screen.context.render();
  assert.equal(screen.part('rate').textContent, '0.0');
});

test('a canary shows shares; a replacement raises the notice', async () => {
  const answer = (version) => liveAnswer(1, 0, {version});
  const canary = await openViewer(['old', 'new', 'old', 'new'].map(answer));
  for (let poll = 0; poll < 3; poll++) await canary.next();
  canary.context.render();
  assert.deepEqual(canary.part('version').children.map((c) => c.textContent),
      ['new 50%', 'old 50%']);
  const swap = await openViewer(
      ['old', 'new', 'new', 'new', 'new'].map(answer));
  for (let poll = 0; poll < 4; poll++) await swap.next();
  swap.context.render();
  assert.equal(swap.part('version-notice').textContent, '버전 변경 old → new');
  assert.equal(swap.part('version').classList.contains('changed'), true);
});

test('versions and chart segments match sample-app', () => {
  // Pinned to the same values as sample-app test/live-page.test.js.
  assert.equal(versionColor('d697a40'), '#a3e635');
  assert.equal(versionColor('a1b2c3d'), '#f472b6');
  assert.deepEqual(splitChartSegments([{environment: 'aws', rate: 1}, null]),
      [{environment: 'aws', firstIndex: 0, rates: [1]}]);
  assert.deepEqual(createVersionTracker().shares(), []);
});
