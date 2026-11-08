'use strict';
// 운영자·에이전트 요청 서명 검사. 키는 전송하지 않는다.
//   X-Demo-Auth: <role>.<unix초>.<nonce>.<hex 서명>
//   서명 = HMAC-SHA256(키, role\nMETHOD\npath\nunix초\nnonce\nsha256hex(body))
// 같은 규칙을 public/hmac.js(브라우저)와 agent/agent.py(온프렘)가 따른다.
const crypto = require('node:crypto');

const HEADER = 'x-demo-auth';
const MAX_SKEW_SECONDS = 30;
const NONCE_PATTERN = /^[A-Za-z0-9_-]{16,64}$/;
const ROLES = new Set(['operator', 'agent']);

function sha256Hex(body) {
  return crypto.createHash('sha256').update(body).digest('hex');
}

function canonical({role, method, path, timestamp, nonce, body}) {
  return [role, method.toUpperCase(), path, String(timestamp), nonce,
    sha256Hex(body)].join('\n');
}

function sign(key, fields) {
  return crypto.createHmac('sha256', key).update(canonical(fields)).digest('hex');
}

function header(key, {role, method, path, body, timestamp, nonce}) {
  const ts = timestamp ?? Math.floor(Date.now() / 1000);
  const n = nonce ?? crypto.randomBytes(16).toString('hex');
  return `${role}.${ts}.${n}.${sign(key, {role, method, path, timestamp: ts, nonce: n, body})}`;
}

/** Remembers nonces until they can no longer pass the clock check. */
function createNonceCache(nowSeconds = () => Math.floor(Date.now() / 1000)) {
  const seen = new Map();
  return {
    remember(nonce) {
      const now = nowSeconds();
      for (const [value, expiry] of seen) {
        if (expiry < now) seen.delete(value);
      }
      if (seen.has(nonce)) return false;
      seen.set(nonce, now + MAX_SKEW_SECONDS * 2 + 1);
      return true;
    },
    size: () => seen.size,
  };
}

/**
 * Checks one request. `keys` maps role → key; a role without a key is closed.
 * Returns {ok: true, role} or {ok: false, code}.
 */
function verify({value, method, path, body, keys, expectedRole, nonces,
  nowSeconds = Math.floor(Date.now() / 1000)}) {
  if (typeof value !== 'string') return {ok: false, code: 'AUTH_MISSING'};
  const parts = value.split('.');
  if (parts.length !== 4) return {ok: false, code: 'AUTH_MALFORMED'};
  const [role, ts, nonce, signature] = parts;
  if (!ROLES.has(role) || role !== expectedRole) {
    return {ok: false, code: 'AUTH_ROLE'};
  }
  const key = keys[role];
  if (!key) return {ok: false, code: 'AUTH_NOT_CONFIGURED'};
  if (!/^\d{1,12}$/.test(ts) || !NONCE_PATTERN.test(nonce) ||
      !/^[0-9a-f]{64}$/.test(signature)) {
    return {ok: false, code: 'AUTH_MALFORMED'};
  }
  if (Math.abs(nowSeconds - Number(ts)) > MAX_SKEW_SECONDS) {
    return {ok: false, code: 'AUTH_CLOCK'};
  }
  const expected = sign(key, {role, method, path, timestamp: ts, nonce, body});
  const a = Buffer.from(expected, 'hex');
  const b = Buffer.from(signature, 'hex');
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) {
    return {ok: false, code: 'AUTH_SIGNATURE'};
  }
  // Only a valid signature may consume a nonce, so forgeries cannot block one.
  if (!nonces.remember(`${role}:${nonce}`)) return {ok: false, code: 'AUTH_REPLAY'};
  return {ok: true, role};
}

module.exports = {HEADER, MAX_SKEW_SECONDS, canonical, createNonceCache,
  header, sha256Hex, sign, verify};
