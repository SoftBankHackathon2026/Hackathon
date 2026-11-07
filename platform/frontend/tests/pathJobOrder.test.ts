import assert from "node:assert/strict";
import { test } from "node:test";
import type { MigrationSummary } from "../src/api/migrations.ts";
import {
  isActive,
  orderedJobs,
  pickJob,
} from "../src/features/environments/pathJobOrder.ts";

function job(
  job_id: string,
  status: MigrationSummary["status"],
  updated_at: string | null = null,
): MigrationSummary {
  return { job_id, status, updated_at } as MigrationSummary;
}

const JOBS = [
  job("forward_001", "completed", "2026-11-05T13:00:00Z"),
  job("return_001", "pending"),
  job("onprem_to_aws_001", "running", "2026-11-07T12:00:00Z"),
  job("rehearsal_001", "waiting_approval", "2026-11-07T13:00:00Z"),
  job("broken_001", "needs_recovery", "2026-11-07T11:00:00Z"),
];

test("진행 중인 작업이 먼저, 그 안에서는 최근 것부터 온다", () => {
  assert.deepEqual(
    orderedJobs(JOBS).map((j) => j.job_id),
    [
      "rehearsal_001",
      "onprem_to_aws_001",
      "broken_001",
      "return_001",
      "forward_001",
    ],
  );
});

test("고른 작업이 없으면 가장 앞의 진행 중 작업을 보여 준다", () => {
  assert.equal(pickJob(JOBS, null)?.job_id, "rehearsal_001");
  assert.equal(pickJob(JOBS, "forward_001")?.job_id, "forward_001");
  assert.equal(pickJob(JOBS, "gone")?.job_id, "rehearsal_001");
  assert.equal(pickJob([], null), null);
});

test("승인 대기도 진행 중으로 센다", () => {
  assert.equal(isActive("waiting_approval"), true);
  assert.equal(isActive("needs_recovery"), false);
  assert.equal(isActive("completed"), false);
});
