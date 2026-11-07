'use strict';
// 상태판과 명령 대기열. 메모리에만 둔다(복제본 1개). 봇의 실제 상태는 봇이 들고 있고,
// 서버는 에이전트의 마지막 보고를 보여 주며 사람이 누른 명령만 전달한다.
const crypto = require('node:crypto');

const TARGETS = ['worker', 'bot'];
const ACTIONS = ['start', 'stop'];
const DEFAULTS = {
  staleMillis: 15000,      // 보고가 이보다 오래되면 버튼을 잠근다
  pickupMillis: 30000,     // 에이전트가 이 안에 가져가지 않으면 만료
  resultMillis: 60000,     // 가져간 뒤 이 안에 결과가 없으면 만료
  keepCommands: 50,
};

const TEXT_LIMIT = 200;

function text(value) {
  return typeof value === 'string' ? value.slice(0, TEXT_LIMIT) : null;
}

function finite(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Keeps only known fields so a report cannot inject markup or bulk data. */
function cleanBot(raw) {
  if (!raw || typeof raw !== 'object') return {reachable: false, error: 'NO_REPORT'};
  return {
    reachable: raw.reachable === true,
    controllable: raw.controllable === true,
    generation: ['running', 'stopped'].includes(raw.generation) ? raw.generation : null,
    mode: ['continuous', 'budget', 'unsupported'].includes(raw.mode) ? raw.mode : null,
    state: text(raw.state),
    target: text(raw.target),
    rate_per_second: finite(raw.rate_per_second),
    pending: finite(raw.pending),
    committed: finite(raw.committed),
    failed: finite(raw.failed),
    generated_total: finite(raw.generated_total),
    generation_limit: finite(raw.generation_limit),
    max_generated: finite(raw.max_generated),
    reason: text(raw.reason),
    error: text(raw.error),
    observed_at: text(raw.observed_at),
  };
}

function cleanLock(raw) {
  const reasons = Array.isArray(raw?.reasons) ?
    raw.reasons.filter((r) => typeof r === 'string').slice(0, 10).map(text) : [];
  return {locked: raw?.locked !== false || reasons.length > 0, reasons};
}

function createControl(options = {}) {
  const cfg = {...DEFAULTS, ...options};
  const now = cfg.now ?? (() => Date.now());
  let report = null;          // {at, bots, lock, agent}
  const commands = [];

  function expire() {
    const t = now();
    for (const c of commands) {
      if (c.status === 'queued' && t - c.created_at > cfg.pickupMillis) {
        c.status = 'expired'; c.error = 'AGENT_DID_NOT_PICK_UP'; c.finished_at = t;
      } else if (c.status === 'sent' && t - c.sent_at > cfg.resultMillis) {
        c.status = 'expired'; c.error = 'NO_RESULT_FROM_AGENT'; c.finished_at = t;
      }
    }
    while (commands.length > cfg.keepCommands) commands.shift();
  }

  function fresh() {
    return report !== null && now() - report.at <= cfg.staleMillis;
  }

  /** Why a command for `target` may not be sent now, or null. */
  function blockReason(target) {
    if (!fresh()) return 'AGENT_STALE';
    if (report.lock.locked) return 'CUTOVER_LOCK';
    if (!report.bots[target]?.controllable) return 'TARGET_NOT_CONTROLLABLE';
    if (commands.some((c) => c.target === target &&
        (c.status === 'queued' || c.status === 'sent'))) {
      return 'COMMAND_IN_PROGRESS';
    }
    return null;
  }

  function submit({target, action}) {
    expire();
    if (!TARGETS.includes(target) || !ACTIONS.includes(action)) {
      return {ok: false, status: 400, code: 'INVALID_COMMAND'};
    }
    const reason = blockReason(target);
    if (reason) return {ok: false, status: 409, code: reason};
    const command = {id: crypto.randomUUID(), target, action, status: 'queued',
      created_at: now(), sent_at: null, finished_at: null, error: null};
    commands.push(command);
    return {ok: true, status: 202, command: view(command)};
  }

  function take() {
    expire();
    const t = now();
    const out = [];
    for (const c of commands) {
      if (c.status === 'queued') {
        c.status = 'sent'; c.sent_at = t;
        out.push({id: c.id, target: c.target, action: c.action});
      }
    }
    return out;
  }

  function acceptReport(raw) {
    if (!raw || typeof raw !== 'object') return {ok: false, code: 'INVALID_REPORT'};
    const t = now();
    report = {
      at: t,
      bots: {worker: cleanBot(raw.bots?.worker), bot: cleanBot(raw.bots?.bot)},
      lock: cleanLock(raw.lock),
      agent: {version: text(raw.agent?.version), host: text(raw.agent?.host)},
    };
    for (const r of Array.isArray(raw.results) ? raw.results : []) {
      const c = commands.find((x) => x.id === r?.id && x.status === 'sent');
      if (!c) continue;
      c.status = r.ok === true ? 'done' : 'rejected';
      c.error = r.ok === true ? null : (text(r.error) ?? 'UNKNOWN');
      c.finished_at = t;
    }
    expire();
    return {ok: true};
  }

  function view(c) {
    return {id: c.id, target: c.target, action: c.action, status: c.status,
      error: c.error, created_at: new Date(c.created_at).toISOString(),
      finished_at: c.finished_at ? new Date(c.finished_at).toISOString() : null};
  }

  function state() {
    expire();
    const t = now();
    return {
      now: new Date(t).toISOString(),
      agent: report ? {fresh: fresh(), last_report_at: new Date(report.at).toISOString(),
        age_seconds: Math.round((t - report.at) / 100) / 10, ...report.agent} :
        {fresh: false, last_report_at: null},
      lock: report ? report.lock : {locked: true, reasons: ['에이전트 보고 없음']},
      bots: report ? report.bots : {worker: cleanBot(null), bot: cleanBot(null)},
      blocked: Object.fromEntries(TARGETS.map((x) => [x, blockReason(x)])),
      commands: commands.slice(-20).reverse().map(view),
    };
  }

  return {submit, take, acceptReport, state};
}

module.exports = {createControl, DEFAULTS};
