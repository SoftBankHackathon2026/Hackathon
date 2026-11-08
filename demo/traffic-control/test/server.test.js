'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const auth = require('../lib/auth');
const browser = require('../public/hmac');
const {createControl} = require('../lib/control');
const {createServer} = require('../server');

const OP = 'o'.repeat(40);
const AG = 'a'.repeat(40);
// Shared with agent/test_agent.py: both implementations must produce this exact header.
const VECTOR = {role: 'agent', method: 'POST', path: '/agent/report',
  body: '{"x":1}', timestamp: 1794000000, nonce: '0123456789abcdef0123'};

test('browser sha256/hmac match node crypto, including unicode and long keys', () => {
  for (const len of [0, 1, 55, 56, 63, 64, 65, 200, 1000]) {
    const data = crypto.randomBytes(len);
    assert.equal(browser.sha256Hex(new Uint8Array(data)),
        crypto.createHash('sha256').update(data).digest('hex'));
  }
  assert.equal(browser.sha256Hex('전환 데모 ✓'),
      crypto.createHash('sha256').update('전환 데모 ✓').digest('hex'));
  for (const key of ['k', 'x'.repeat(64), 'y'.repeat(65), '키'.repeat(40)]) {
    assert.equal(browser.hmacHex(key, 'msg\n본문'),
        crypto.createHmac('sha256', key).update('msg\n본문').digest('hex'));
  }
});

test('browser and server produce the same header for the shared vector', () => {
  const expected = 'agent.1794000000.0123456789abcdef0123.' +
    '07e3f2e5208ec2cfce9f47329b4504478b128c9e282e7e5e6af99c952679ef8f';
  assert.equal(auth.header(AG, VECTOR), expected);
  assert.equal(browser.authHeader(AG, VECTOR), expected);
});

test('verify rejects wrong role, bad signature, old clock and replays', () => {
  const nonces = auth.createNonceCache(() => 1000);
  const keys = {operator: OP, agent: AG};
  const base = {method: 'POST', path: '/api/commands', body: '{}', keys, nonces, nowSeconds: 1000};
  const good = auth.header(OP, {role: 'operator', method: 'POST', path: '/api/commands',
    body: '{}', timestamp: 1000, nonce: 'n'.repeat(16)});
  assert.deepEqual(auth.verify({...base, value: good, expectedRole: 'operator'}),
      {ok: true, role: 'operator'});
  assert.equal(auth.verify({...base, value: good, expectedRole: 'operator'}).code, 'AUTH_REPLAY');
  assert.equal(auth.verify({...base, value: good, expectedRole: 'agent'}).code, 'AUTH_ROLE');
  const old = auth.header(OP, {role: 'operator', method: 'POST', path: '/api/commands',
    body: '{}', timestamp: 900, nonce: 'm'.repeat(16)});
  assert.equal(auth.verify({...base, value: old, expectedRole: 'operator'}).code, 'AUTH_CLOCK');
  const tampered = auth.header(OP, {role: 'operator', method: 'POST', path: '/api/commands',
    body: '{"a":1}', timestamp: 1000, nonce: 'p'.repeat(16)});
  assert.equal(auth.verify({...base, value: tampered, expectedRole: 'operator'}).code,
      'AUTH_SIGNATURE');
  const agentKeyForOperator = auth.header(AG, {role: 'operator', method: 'POST',
    path: '/api/commands', body: '{}', timestamp: 1000, nonce: 'q'.repeat(16)});
  assert.equal(auth.verify({...base, value: agentKeyForOperator, expectedRole: 'operator'}).code,
      'AUTH_SIGNATURE');
  // A forged header must not burn the nonce of a later valid request.
  const nonce = 'r'.repeat(16);
  const forged = `operator.1000.${nonce}.${'0'.repeat(64)}`;
  assert.equal(auth.verify({...base, value: forged, expectedRole: 'operator'}).code, 'AUTH_SIGNATURE');
  const real = auth.header(OP, {role: 'operator', method: 'POST', path: '/api/commands',
    body: '{}', timestamp: 1000, nonce});
  assert.equal(auth.verify({...base, value: real, expectedRole: 'operator'}).ok, true);
  assert.equal(auth.verify({...base, keys: {operator: null}, value: real,
    expectedRole: 'operator'}).code, 'AUTH_NOT_CONFIGURED');
});

function report(overrides = {}) {
  return {
    agent: {version: 't'},
    lock: {locked: false, reasons: []},
    bots: {
      bot: {reachable: true, controllable: true, mode: 'continuous', generation: 'stopped'},
    },
    route: {known: true, target: 'onprem', mode: 'running'},
    ...overrides,
  };
}

test('command flow: report → submit → take → result', () => {
  let t = 0;
  const c = createControl({now: () => t});
  assert.equal(c.submit({target: 'bot', action: 'start'}).code, 'AGENT_STALE');
  c.acceptReport(report());
  const sent = c.submit({target: 'bot', action: 'start'});
  assert.equal(sent.status, 202);
  assert.equal(c.submit({target: 'bot', action: 'stop'}).code, 'COMMAND_IN_PROGRESS');
  assert.equal(c.submit({target: 'bot', action: 'explode'}).code, 'INVALID_COMMAND');
  const taken = c.take();
  assert.deepEqual(taken, [{id: sent.command.id, target: 'bot', action: 'start'}]);
  assert.deepEqual(c.take(), []);
  t = 1000;
  c.acceptReport(report({results: [{id: sent.command.id, ok: true}]}));
  assert.equal(c.state().commands[0].status, 'done');
  assert.equal(c.submit({target: 'bot', action: 'stop'}).status, 202);
});

test('locks: cutover, stale agent, uncontrollable target, missing lock field', () => {
  let t = 0;
  const c = createControl({now: () => t});
  c.acceptReport(report({lock: {locked: true, reasons: ['커넥터 1개']}}));
  assert.equal(c.submit({target: 'bot', action: 'stop'}).code, 'CUTOVER_LOCK');
  c.acceptReport(report({lock: undefined}));
  assert.equal(c.state().lock.locked, true, 'missing lock info must fail closed');
  c.acceptReport(report({bots: {bot: {reachable: true, controllable: false, mode: 'unsupported'}}}));
  assert.equal(c.submit({target: 'bot', action: 'stop'}).code, 'TARGET_NOT_CONTROLLABLE');
  // The retired INSERT worker is not a command target any more.
  assert.equal(c.submit({target: 'worker', action: 'stop'}).code, 'INVALID_COMMAND');
  c.acceptReport(report());
  t = 16000;
  assert.equal(c.submit({target: 'bot', action: 'stop'}).code, 'AGENT_STALE');
});

test('commands expire when the agent never picks them up or never answers', () => {
  let t = 0;
  const c = createControl({now: () => t});
  c.acceptReport(report());
  const a = c.submit({target: 'bot', action: 'stop'}).command;
  t = 31000;
  c.acceptReport(report());
  assert.equal(c.state().commands.find((x) => x.id === a.id).status, 'expired');
  const b = c.submit({target: 'bot', action: 'stop'}).command;
  c.take();
  t = 92000;
  c.acceptReport(report());
  const view = c.state().commands.find((x) => x.id === b.id);
  assert.equal(view.status, 'expired');
  assert.equal(view.error, 'NO_RESULT_FROM_AGENT');
});

test('report fields are whitelisted', () => {
  const c = createControl();
  c.acceptReport(report({bots: {bot: {reachable: true, reason: '<b>x</b>'.repeat(100),
    secret: 'leak', rate_per_second: 'NaN'}}, route: {known: true, target: 'x'.repeat(300),
    token: 'leak'}}));
  const b = c.state().bots.bot;
  assert.equal(b.secret, undefined);
  assert.equal(b.rate_per_second, null);
  assert.equal(b.reason.length, 200);
  assert.equal(c.state().bots.worker, undefined);
  const route = c.state().route;
  assert.equal(route.token, undefined);
  assert.equal(route.target.length, 200);
  c.acceptReport(report({route: {target: 'onprem'}}));
  assert.deepEqual(c.state().route, {known: false}, 'route without known=true is unknown');
});

async function withServer(fn) {
  const server = createServer({keys: {operator: OP, agent: AG}});
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    await fn(base);
  } finally {
    await new Promise((r) => server.close(r));
  }
}

function signed(key, role, method, path, body = '') {
  return {method, body: body || undefined, headers: {'Content-Type': 'application/json',
    'X-Demo-Auth': auth.header(key, {role, method, path, body})}};
}

test('HTTP: public state, signed agent/operator routes, static files', async () => {
  await withServer(async (base) => {
    assert.equal((await fetch(`${base}/api/state`)).status, 200);
    const page = await fetch(`${base}/`);
    assert.equal(page.status, 200);
    assert.match(page.headers.get('content-security-policy'), /default-src 'self'/);
    assert.equal((await fetch(`${base}/agent/commands`)).status, 401);
    assert.equal((await fetch(`${base}/nope`)).status, 404);

    const body = JSON.stringify(report());
    const r1 = await fetch(`${base}/agent/report`, signed(AG, 'agent', 'POST', '/agent/report', body));
    assert.equal(r1.status, 200);

    const cmdBody = JSON.stringify({target: 'bot', action: 'start'});
    const wrongKey = await fetch(`${base}/api/commands`,
        signed(AG, 'operator', 'POST', '/api/commands', cmdBody));
    assert.equal(wrongKey.status, 401);
    const ok = await fetch(`${base}/api/commands`,
        signed(OP, 'operator', 'POST', '/api/commands', cmdBody));
    assert.equal(ok.status, 202);

    const taken = await (await fetch(`${base}/agent/commands`,
        signed(AG, 'agent', 'GET', '/agent/commands'))).json();
    assert.equal(taken.commands.length, 1);
    assert.equal(taken.commands[0].target, 'bot');

    const withQuery = await fetch(`${base}/agent/commands?x=1`,
        signed(AG, 'agent', 'GET', '/agent/commands'));
    assert.equal(withQuery.status, 400);
  });
});

test('HTTP: routes stay closed without keys', async () => {
  const server = createServer({keys: {operator: null, agent: null}});
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    assert.equal((await fetch(`${base}/api/commands`, {method: 'POST', body: '{}'})).status, 503);
    assert.equal((await fetch(`${base}/agent/commands`)).status, 503);
  } finally {
    await new Promise((r) => server.close(r));
  }
});

test('start is refused while the receipt queue does not deliver; stop stays allowed', () => {
  const c = createControl();
  c.acceptReport(report({route: {known: true, target: 'onprem', mode: 'paused'}}));
  assert.equal(c.submit({target: 'bot', action: 'start'}).code, 'INGRESS_PAUSED');
  assert.equal(c.state().start_blocked.bot, 'INGRESS_PAUSED');
  assert.equal(c.state().blocked.bot, null);
  assert.equal(c.submit({target: 'bot', action: 'stop'}).status, 202);

  const unknown = createControl();
  unknown.acceptReport(report({route: {known: false}}));
  assert.equal(unknown.submit({target: 'bot', action: 'start'}).code, 'ROUTE_UNKNOWN');

  const running = createControl();
  running.acceptReport(report());
  assert.equal(running.state().start_blocked.bot, null);
  assert.equal(running.submit({target: 'bot', action: 'start'}).status, 202);
});

test('the cutover lock still wins over the queue state for start', () => {
  const c = createControl();
  c.acceptReport(report({lock: {locked: true, reasons: ['x']},
    route: {known: true, target: 'onprem', mode: 'paused'}}));
  assert.equal(c.submit({target: 'bot', action: 'start'}).code, 'CUTOVER_LOCK');
});
