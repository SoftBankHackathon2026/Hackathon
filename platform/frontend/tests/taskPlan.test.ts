import assert from "node:assert/strict";
import { test } from "node:test";
import type { PlanStep } from "../src/api/taskPlan.ts";
import {
  STEP_STATUS_LABELS,
  STEP_STATUS_TONES,
  stepNote,
} from "../src/features/migrations/taskPlan.ts";

function step(over: Partial<PlanStep>): PlanStep {
  return {
    id: "images",
    title: "이미지 이관",
    description: "",
    tool: "migration.image_migration",
    status: "pending",
    runnable: false,
    ...over,
  };
}

test("지원하지 않는 방향은 그렇게 알린다", () => {
  assert.equal(
    stepNote(step({ status: "unsupported", tool: null })),
    "이 방향은 아직 지원하지 않아요.",
  );
  assert.equal(STEP_STATUS_LABELS.unsupported, "지원 안 함");
});

test("플랫폼이 아직 못 돌리는 대기 단계만 직접 실행을 안내한다", () => {
  assert.match(stepNote(step({}))!, /직접 실행/);
  assert.equal(stepNote(step({ runnable: true })), null);
  assert.equal(stepNote(step({ status: "done" })), null);
});

test("단계 상태마다 색이 정해져 있다", () => {
  assert.equal(STEP_STATUS_TONES.waiting, "warn");
  assert.equal(STEP_STATUS_TONES.failed, "bad");
  assert.equal(STEP_STATUS_TONES.done, "ok");
});
