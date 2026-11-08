import assert from "node:assert/strict";
import { test } from "node:test";
import {
  isActive,
  isApprovalStage,
  isPauseStage,
  pollInterval,
  stageDurationSeconds,
  stageNotes,
  stageSummaryText,
  stepLabel,
  stageTitle,
  statusTone,
} from "../src/features/migrations/stages.ts";

test("stepLabel counts the current stage as one based", () => {
  assert.equal(
    stepLabel({ status: "running", stage_index: 3, stage_count: 10 }),
    "4/10",
  );
  assert.equal(
    stepLabel({ status: "completed", stage_index: 9, stage_count: 10 }),
    "10/10",
  );
  assert.equal(
    stepLabel({ status: "pending", stage_index: 0, stage_count: 10 }),
    "0/10",
  );
});

test("stageSummaryText names the approval wait explicitly", () => {
  const waiting = {
    status: "waiting_approval",
    stage: "READY",
    stage_index: 3,
    stage_count: 10,
  } as const;
  assert.equal(stageSummaryText(waiting), "4/10 전환 승인 대기");
  assert.equal(
    stageSummaryText({
      ...waiting,
      status: "running",
      stage: "COPYING",
      stage_index: 1,
    }),
    "2/10 초기 복사",
  );
});

test("stage durations use the clock only while a stage is open", () => {
  const now = Date.parse("2026-11-05T00:01:00Z");
  assert.equal(
    stageDurationSeconds(
      { started_at: null, finished_at: null, status: "pending" },
      now,
    ),
    null,
  );
  assert.equal(
    stageDurationSeconds(
      {
        started_at: "2026-11-05T00:00:00Z",
        finished_at: "2026-11-05T00:00:30Z",
        status: "done",
      },
      now,
    ),
    30,
  );
  assert.equal(
    stageDurationSeconds(
      {
        started_at: "2026-11-05T00:00:00Z",
        finished_at: null,
        status: "current",
      },
      now,
    ),
    60,
  );
  assert.equal(
    stageDurationSeconds(
      { started_at: "2026-11-05T00:00:00Z", finished_at: null, status: "done" },
      now + 3_600_000,
    ),
    null,
  );
});

test("stage notes flag the approval point and the point of no return", () => {
  assert.deepEqual(stageNotes({ name: "READY", point_of_no_return: false }), [
    "승인 지점",
  ]);
  assert.deepEqual(
    stageNotes({ name: "SWITCHING", point_of_no_return: true }),
    ["되돌릴 수 없는 지점"],
  );
  assert.deepEqual(
    stageNotes({ name: "COPYING", point_of_no_return: false }),
    [],
  );
});

test("polling is fast only while a migration is moving", () => {
  assert.equal(pollInterval("running"), 5000);
  assert.equal(pollInterval("waiting_approval"), 5000);
  for (const status of [
    "pending",
    "completed",
    "aborted",
    "needs_recovery",
    "rolled_back",
  ] as const)
    assert.equal(pollInterval(status), 30000);
  assert.equal(pollInterval(undefined), 30000);
  assert.equal(isActive("running"), true);
  assert.equal(isActive("completed"), false);
});

test("pause window stages are FENCING through SWITCHING only", () => {
  for (const s of ["FENCING", "draining", "SYNC_CONFIRMED", "Switching"])
    assert.equal(isPauseStage(s), true, s);
  for (const s of ["READY", "VERIFYING", "COPYING", "COMPLETED"])
    assert.equal(isPauseStage(s), false, s);
  assert.equal(isApprovalStage("ready"), true);
});

test("stage titles and status tones", () => {
  assert.equal(stageTitle("fencing"), "원본 쓰기 차단");
  assert.equal(stageTitle("CUSTOM"), "CUSTOM");
  assert.equal(statusTone("completed"), "ok");
  assert.equal(statusTone("needs_recovery"), "bad");
  assert.equal(statusTone("waiting_approval"), "warn");
});
