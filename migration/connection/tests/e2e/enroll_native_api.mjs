/** Authenticated API fallback for a real native flow when desktop clipboard delivery is delayed.
 * This deliberately does not claim to test browser copy/paste. No database or readiness mutation.
 * Random seed and the product bootstrap command remain in memory/private process stdin only.
 */
import { randomBytes } from 'node:crypto';
import { readFileSync, statSync, openSync, closeSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const root = '/private/tmp/onprem-live-e2e';
const require = createRequire(new URL('../../../../platform/frontend/package.json', import.meta.url));
const { ml_dsa65 } = await import(pathToFileURL(require.resolve('@noble/post-quantum/ml-dsa.js')).href);
let seed;
let command;
let cookie = '';
try {
  if ((statSync(root).mode & 0o077) !== 0 || (statSync(join(root, 'browser-session.cookies')).mode & 0o077) !== 0) {
    throw new Error('Private directory and session cookie permissions are required');
  }
  const metadata = JSON.parse(readFileSync(join(root, 'public/release-metadata.json'), 'utf8'));
  const rows = readFileSync(join(root, 'browser-session.cookies'), 'utf8').split('\n');
  const cookies = [];
  for (let row of rows) {
    if (row.startsWith('#HttpOnly_')) row = row.slice('#HttpOnly_'.length);
    else if (!row || row.startsWith('#')) continue;
    const parts = row.split('\t');
    if (parts.length !== 7 || !['127.0.0.1', 'localhost'].includes(parts[0])) continue;
    if (parts[4] !== '0' && Number(parts[4]) * 1000 <= Date.now()) continue;
    cookies.push(`${parts[5]}=${parts[6]}`);
  }
  cookie = cookies.join('; ');
  if (!cookie) throw new Error('Normal logged-in session is unavailable');
  seed = randomBytes(32);
  const keys = ml_dsa65.keygen(seed);
  const publicKey = Buffer.from(keys.publicKey).toString('base64url');
  keys.secretKey.fill(0);
  const operationId = randomBytes(16).toString('base64url');
  const response = await fetch('http://127.0.0.1:18320/api/v1/connections', {
    method: 'POST', redirect: 'error', signal: AbortSignal.timeout(10000),
    headers: { 'Content-Type': 'application/json', Origin: 'http://localhost:4327', Cookie: cookie },
    body: JSON.stringify({ publicKey, operationId }),
  });
  cookie = '';
  if (response.status !== 201) throw new Error(`Normal enrollment request failed with HTTP ${response.status}`);
  const issued = await response.json();
  const requestId = issued.id;
  const bootstrapUrl = issued.deployment?.bootstrapUrl;
  if (!/^[A-Za-z0-9_-]{22}$/.test(requestId) || bootstrapUrl !== metadata.bootstrap_url ||
      !/^https:\/\/[A-Za-z0-9.-]+(?::[0-9]+)?\/[A-Za-z0-9/_.-]+$/.test(bootstrapUrl)) {
    throw new Error('Unexpected public enrollment response');
  }
  // Same command grammar as the product; its bootstrap immediately unsets MIGRATION_ENROLL.
  command = Buffer.from(`curl -qfsS --proto '=https' ${bootstrapUrl} | MIGRATION_ENROLL='${requestId}.${seed.toString('base64url')}' bash\n`);
  seed.fill(0);
  if (command.length > 257) throw new Error('Product bootstrap command limit exceeded');
  const logPath = join(root, `native-api-enrollment-${Date.now()}.log`);
  const descriptor = openSync(logPath, 'wx', 0o600);
  let child;
  try {
    child = spawn('docker', ['exec', '-i', '-u', 'connector', 'mc-live-e2e-client', 'bash'], {
      detached: true, stdio: ['pipe', descriptor, descriptor],
    });
    await new Promise((resolve, reject) => {
      child.once('error', reject);
      child.stdin.once('error', reject);
      child.stdin.end(command, resolve);
    });
    command.fill(0);
    child.unref();
  } finally { closeSync(descriptor); }
  const evidence = { request_id: requestId, log_path: logPath, path: 'authenticated_api_fallback',
    browser_clipboard_tested: false, issued_at_ms: Date.now(), approval: 'must be performed in the real browser' };
  writeFileSync(join(root, 'native-api-enrollment-request.json'), JSON.stringify(evidence, null, 2), { mode: 0o600 });
  console.log(JSON.stringify({ request_id: requestId, log_path: logPath }));
} catch (error) {
  // Never print response objects, request headers, command strings or key material.
  console.error(`NATIVE_API_ENROLLMENT_FAILED: ${error instanceof Error ? error.message : 'unknown stage'}`);
  process.exitCode = 1;
} finally {
  seed?.fill(0);
  command?.fill(0);
  cookie = '';
}
