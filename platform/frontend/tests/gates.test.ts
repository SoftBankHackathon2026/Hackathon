import assert from "node:assert/strict";
import { test } from "node:test";
import type { Gate } from "../src/api/migrations.ts";
import {
  eventTitle,
  latestGates,
  outcomeTone,
  reasonLabel,
} from "../src/features/migrations/gates.ts";

function gate(
  stage: Gate["stage"],
  at: string,
  outcome: Gate["outcome"],
): Gate {
  return {
    decision_id: `${stage}-${at}`,
    stage,
    outcome,
    reason_codes: [],
    policy_version: "migration-policy-v1",
    evaluated_at: at,
  };
}

test("reasonLabel explains known rule codes and falls back to the raw code", () => {
  assert.equal(reasonLabel("SEC-01"), "대상 DB가 외부에 노출됨");
  assert.equal(
    reasonLabel("APP-01-TARGET-DB-UNREACHABLE"),
    "대상 앱의 DB 연결 또는 주문 조회 실패",
  );
  assert.equal(
    reasonLabel("OPS-01-UNVERIFIED"),
    "최종 동기화가 끝나지 않음 (미확인)",
  );
  assert.equal(reasonLabel("EVIDENCE-STALE"), "근거가 오래됨");
  assert.equal(reasonLabel("WHATEVER"), "WHATEVER");
});

test("latestGates keeps the newest decision per stage", () => {
  const latest = latestGates([
    gate("pre_cutover", "2026-11-05T00:00:00Z", "deny"),
    gate("pre_cutover", "2026-11-05T00:05:00Z", "allow"),
    gate("plan", "2026-11-05T00:01:00Z", "allow"),
  ]);
  assert.equal(latest.size, 2);
  assert.equal(latest.get("pre_cutover")?.outcome, "allow");
});

test("outcomeTone maps each outcome to a distinct tone", () => {
  assert.equal(outcomeTone("allow"), "ok");
  assert.equal(outcomeTone("deny"), "bad");
  assert.equal(outcomeTone("hold"), "warn");
  assert.equal(outcomeTone("invalid"), "idle");
});

test("eventTitle makes gate events readable and leaves other titles alone", () => {
  assert.equal(
    eventTitle("정책 게이트 final_verify: allow"),
    "정책 게이트 · 최종 검증 단계 허용",
  );
  assert.equal(
    eventTitle("정책 게이트 pre_cutover: hold"),
    "정책 게이트 · 전환 전 단계 보류",
  );
  assert.equal(eventTitle("전환 시작"), "전환 시작");
});
