import assert from "node:assert/strict";
import { test } from "node:test";
import type {
  AgentRun,
  AgentRunDetail,
  AgentRunEvent,
  AgentRunState,
} from "../src/api/agent.ts";
import {
  agentScore,
  foldSteps,
  investigation,
  runSeconds,
  verdict,
} from "../src/features/agent/investigation.ts";

const DENY = {
  outcome: "deny",
  reason_codes: ["APP-01-ORDER-CHECK-FAILED"],
};

function ev(at: string, state: string, message: string): AgentRunEvent {
  return { at: `2026-11-07T08:${at}Z`, state, message };
}

function detail(
  state: AgentRunState,
  detail: Record<string, unknown>,
  events: AgentRunEvent[],
): AgentRunDetail {
  return {
    run_id: "run-abc123",
    agent_id: "agent-a",
    job_id: "job",
    state,
    created_at: "2026-11-07T08:19:23Z",
    updated_at: events.at(-1)?.at ?? "2026-11-07T08:19:23Z",
    summary: { reason_codes: [], changes: [], diagnosis: null },
    detail,
    events,
    pending_approval: null,
    can_decide: false,
    can_rerun: false,
    can_delete: false,
  };
}

const PROPOSAL = {
  cause: "DB_NAME 이 계획과 달라요",
  impact: "대상 DB로 연결해요",
  verification: "다시 읽어 확인했어요",
  changes: [{ key: "DB_NAME", value: "app_return", before: "app_prod" }],
};

const resolved = detail(
  "RESOLVED",
  {
    pre_decision: DENY,
    diagnosis: {
      ok: false,
      step: "login",
      detail: "DB 가 없음 (SQLSTATE 3D000)",
    },
    proposal: PROPOSAL,
    expected_version: 13,
    probe: { ok: true, step: "done", error: null },
    applied_version: 14,
    post_decision: { outcome: "allow", reason_codes: [] },
  },
  [
    ev("19:23", "CHECKING", "실행 시작"),
    ev("19:53", "WAITING_APPROVAL", "승인 대기"),
    ev("20:28", "APPLYING", "승인됨 (유빈, 플랫폼)"),
    ev("20:28", "APPLYING", "저장소 반영 v13 → v14"),
    ev("21:39", "RESOLVED", "복구 완료. 재검증 OPA allow (저장소 v14)."),
  ],
);

function byKey(run: AgentRunDetail) {
  return Object.fromEntries(investigation(run).map((s) => [s.key, s]));
}

test("resolved run walks every step with evidence", () => {
  const steps = byKey(resolved);
  assert.deepEqual(
    investigation(resolved).map((s) => [s.key, s.tone, s.reached]),
    [
      ["detect", "bad", true],
      ["diagnose", "ok", true],
      ["propose", "ok", true],
      ["probe", "ok", true],
      ["approve", "ok", true],
      ["apply", "ok", true],
      ["verify", "ok", true],
    ],
  );
  assert.deepEqual(steps.detect.codes, ["APP-01-ORDER-CHECK-FAILED"]);
  assert.match(steps.diagnose.text, /로그인 단계에서 실패해요: DB 가 없음/);
  assert.equal(steps.propose.text, "DB_NAME 값을 바꾸자고 제안했어요.");
  assert.deepEqual(steps.approve.notes, [
    { label: "기다린 시간", text: "35초" },
  ]);
  assert.equal(
    steps.apply.text,
    "설정 저장소 버전을 v13에서 v14로 올리고 앱을 다시 시작했어요.",
  );
  assert.equal(steps.verify.at, resolved.updated_at);
  assert.equal(runSeconds(resolved), 136);
  const v = verdict(resolved);
  assert.equal(v.tone, "ok");
  assert.match(v.text, /DB_NAME 값을 app_return 값으로 바꿔서/);
});

test("probe failure stops before writing", () => {
  const run = detail(
    "PROBE_FAILED",
    {
      pre_decision: DENY,
      proposal: PROPOSAL,
      probe: { ok: false, step: "login", error: "ConnectionTimeout" },
    },
    [
      ev("19:23", "CHECKING", "실행 시작"),
      ev("19:50", "WAITING_APPROVAL", "승인 대기"),
      ev("20:10", "APPLYING", "승인됨"),
      ev("20:18", "PROBE_FAILED", "수정 값 탐침 실패"),
    ],
  );
  const steps = byKey(run);
  assert.equal(steps.diagnose.reached, false);
  assert.match(steps.diagnose.text, /진단 기록을 남기지 않았어요/);
  assert.equal(steps.probe.tone, "bad");
  assert.match(
    steps.probe.text,
    /로그인 단계에서 실패했어요 \(ConnectionTimeout\)/,
  );
  assert.equal(steps.apply.reached, false);
  assert.equal(steps.verify.reached, false);
  assert.equal(verdict(run).tone, "bad");
});

test("static rejection marks the proposal step", () => {
  const run = detail(
    "REJECTED",
    { pre_decision: DENY, rejections: ["S0-EMPTY"] },
    [
      ev("19:23", "CHECKING", "실행 시작"),
      ev("19:43", "REJECTED", "정적 검사 거절: 제안이 비어 있다"),
    ],
  );
  const steps = byKey(run);
  assert.equal(steps.propose.tone, "bad");
  assert.deepEqual(steps.propose.codes, ["S0-EMPTY"]);
  assert.equal(steps.propose.text, "정적 검사 거절: 제안이 비어 있다");
  assert.equal(steps.approve.reached, false);
});

test("recheck rejection after approval lands on the probe step", () => {
  const run = detail(
    "REJECTED",
    { pre_decision: DENY, proposal: PROPOSAL, rejections: ["S3-KEY"] },
    [
      ev("19:23", "CHECKING", "실행 시작"),
      ev("19:50", "WAITING_APPROVAL", "승인 대기"),
      ev("20:10", "APPLYING", "승인됨"),
      ev("20:11", "REJECTED", "승인 시 재검사 거절: 키가 범위 밖"),
    ],
  );
  const steps = byKey(run);
  assert.equal(steps.propose.tone, "ok");
  assert.equal(steps.approve.tone, "ok");
  assert.equal(steps.probe.tone, "bad");
  assert.deepEqual(steps.probe.codes, ["S3-KEY"]);
});

test("human rejection and waiting approval", () => {
  const refused = detail(
    "REJECTED",
    { pre_decision: DENY, proposal: PROPOSAL },
    [
      ev("19:50", "WAITING_APPROVAL", "승인 대기"),
      ev("20:00", "REJECTED", "사람이 거절함 (유빈, 플랫폼)"),
    ],
  );
  assert.equal(byKey(refused).approve.tone, "warn");
  const waiting = detail(
    "WAITING_APPROVAL",
    { pre_decision: DENY, proposal: PROPOSAL },
    [ev("19:50", "WAITING_APPROVAL", "승인 대기")],
  );
  assert.equal(byKey(waiting).approve.tone, "running");
  assert.equal(runSeconds(waiting), null);
  assert.match(verdict(waiting).text, /바꾸자고 제안했어요/);
  const auto = detail("APPLYING", { pre_decision: DENY, proposal: PROPOSAL }, [
    ev("19:50", "APPLYING", "자동 적용"),
  ]);
  assert.match(byKey(auto).approve.text, /자동 적용 모드/);
  assert.equal(byKey(auto).verify.tone, "running");
});

test("handed off run stops after detection", () => {
  const run = detail(
    "HANDED_OFF",
    { pre_decision: { outcome: "hold", reason_codes: ["APP-01-UNVERIFIED"] } },
    [ev("19:50", "HANDED_OFF", "AI 범위 밖. 사람에게 넘긴다")],
  );
  const reached = investigation(run).filter((s) => s.reached);
  assert.deepEqual(
    reached.map((s) => s.key),
    ["detect"],
  );
  assert.match(byKey(run).diagnose.text, /범위가 아니어서/);
  assert.match(byKey(run).detect.text, /보류했어요/);
  assert.match(byKey(resolved).detect.text, /문제를 찾았어요 \(OPA deny\)/);
  const folded = foldSteps(investigation(run), true);
  assert.deepEqual(
    folded.shown.map((s) => s.key),
    ["detect"],
  );
  assert.equal(folded.folded, 6);
  assert.equal(foldSteps(investigation(run), false).folded, 0);
  const middle = foldSteps(investigation(resolved), true);
  assert.equal(middle.shown.length, 7);
  assert.equal(middle.folded, 0);
  const healthy = detail(
    "HANDED_OFF",
    { pre_decision: DENY, diagnosis: { ok: true, step: "done", detail: "" } },
    [ev("19:50", "HANDED_OFF", "지금 설정으로 DB 에 접속된다")],
  );
  assert.equal(byKey(healthy).diagnose.tone, "warn");
  assert.equal(verdict(healthy).title, "사람에게 넘겼어요");
});

test("restart failure and cas conflict mark the apply step", () => {
  const restart = detail(
    "RESTART_FAILED",
    { proposal: PROPOSAL, expected_version: 3, applied_version: 4 },
    [ev("20:00", "RESTART_FAILED", "재시작 실패")],
  );
  assert.equal(byKey(restart).apply.tone, "bad");
  assert.equal(byKey(restart).apply.text, "재시작 실패");
  const cas = detail("CAS_CONFLICT", { proposal: PROPOSAL }, [
    ev("20:00", "CAS_CONFLICT", "승인 대기 중 값이 바뀌었다"),
  ]);
  assert.equal(byKey(cas).apply.tone, "bad");
});

function row(state: AgentRunState, seconds: number, changes = 0): AgentRun {
  const start = Date.parse("2026-11-07T08:00:00Z");
  return {
    run_id: `run-${state}-${seconds}`,
    agent_id: "a",
    job_id: "j",
    state,
    created_at: new Date(start).toISOString(),
    updated_at: new Date(start + seconds * 1000).toISOString(),
    summary: {
      reason_codes: [],
      changes: Array.from({ length: changes }, () => ({
        key: "K",
        value: "v",
      })),
      diagnosis: null,
    },
    can_delete: true,
  };
}

test("score counts fixes, median time and human work", () => {
  const score = agentScore([
    row("NOTHING_TO_FIX", 5),
    row("RESOLVED", 120, 1),
    row("RESOLVED", 60, 1),
    row("RESOLVED", 300, 1),
    row("REJECTED", 10, 1),
    row("HANDED_OFF", 30),
    row("WAITING_APPROVAL", 30, 1),
  ]);
  assert.deepEqual(score, {
    detected: 6,
    proposed: 5,
    resolved: 3,
    handedOff: 1,
    waiting: 1,
    medianSeconds: 120,
  });
  assert.equal(
    agentScore([row("RESOLVED", 60), row("RESOLVED", 121)]).medianSeconds,
    91,
  );
  assert.equal(agentScore([]).medianSeconds, null);
  assert.equal(agentScore([row("RESOLVED", 60)]).proposed, 1);
});
