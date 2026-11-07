import assert from "node:assert/strict";
import { test } from "node:test";
import type { MigrationSummary } from "../src/api/migrations.ts";
import type { Posture } from "../src/api/security.ts";
import {
  fleetVerify,
  jobCounts,
  jobVerify,
  longestPause,
  postureCompare,
  writeTotals,
  failedWrites,
} from "../src/features/migrations/summary.ts";

function summary(
  failed: number,
  pause: number,
  maxLatency: number,
  mismatch: number,
): MigrationSummary {
  return {
    job_id: "j",
    direction: "forward",
    source_env: "a",
    target_env: "b",
    data_source: "onprem",
    data_target: "aws",
    stage: "COMPLETED",
    status: "completed",
    stage_index: 9,
    stage_count: 10,
    updated_at: "2026-11-05T00:00:00Z",
    writes: {
      committed_source: 0,
      committed_target: 0,
      failed,
      rejected: 0,
      mismatch,
    },
    downtime: {
      write_pause_seconds: pause,
      failed_requests: failed,
      max_latency_seconds: maxLatency,
      p95_latency_seconds: maxLatency / 2,
    },
  };
}

function withStatus(m: MigrationSummary, status: MigrationSummary["status"]) {
  return { ...m, status };
}

test("jobCounts separates active and waiting jobs", () => {
  const base = summary(0, 0, 0, 0);
  const counts = jobCounts([
    withStatus(base, "running"),
    withStatus(base, "waiting_approval"),
    withStatus(base, "completed"),
  ]);
  assert.deepEqual(counts, { total: 3, active: 2, waiting: 1 });
});

test("writeTotals sums failed and rejected writes", () => {
  assert.deepEqual(writeTotals([summary(1, 0, 0, 0), summary(2, 0, 0, 0)]), {
    failed: 6,
    rejected: 0,
  });
  const base = summary(0, 0, 0, 0);
  const split = {
    ...base,
    writes: { ...base.writes, failed: 4 },
    downtime: { ...base.downtime, failed_requests: 1 },
  };
  assert.equal(failedWrites(split), 5);
  assert.deepEqual(writeTotals([split]), { failed: 5, rejected: 0 });
});

test("longestPause names the job and ignores jobs without a pause", () => {
  const a = { ...summary(0, 2.5, 0, 0), job_id: "a" };
  const b = { ...summary(0, 6, 0, 0), job_id: "b" };
  const none = { ...summary(0, 0, 0, 0), job_id: "n" };
  none.downtime.write_pause_seconds = null;
  assert.deepEqual(longestPause([a, b, none]), { seconds: 6, jobId: "b" });
  assert.equal(longestPause([none]), null);
});

test("fleetVerify counts verified jobs and sums every mismatch", () => {
  const unverified = { ...summary(0, 0, 0, 0), verify_mismatch: null };
  const verified = { ...summary(0, 0, 0, 3), verify_mismatch: 2 };
  assert.deepEqual(fleetVerify([unverified, verified]), {
    checked: 1,
    total: 2,
    mismatch: 5,
  });
});

test("jobVerify prefers the post report and flags any difference", () => {
  const report = (b: number, passed = true) => ({
    passed,
    counts: { B_minus_T: b, T_minus_B: 0, field_mismatch: 0, S_minus_T: 0 },
    source_count: 1,
    target_count: 1,
    committed_count: 1,
  });
  assert.equal(jobVerify(null, null).state, "none");
  assert.deepEqual(jobVerify(report(0), null), {
    state: "pass",
    mismatch: 0,
    phase: "전환 전",
  });
  assert.equal(jobVerify(report(0), report(2)).state, "fail");
  assert.equal(jobVerify(report(0, false), null).state, "fail");
});

test("postureCompare reads scores of the migrated pair", () => {
  assert.equal(postureCompare(undefined), null);
  const posture = {
    parity: {
      source_env: "a",
      target_env: "b",
      regressions: ["x"],
      improvements: ["y", "z"],
    },
    environments: [
      { env_id: "a", score: 57 },
      { env_id: "b", score: 72 },
    ],
  } as unknown as Posture;
  assert.deepEqual(postureCompare(posture), {
    regressions: 1,
    improvements: 2,
    from: 57,
    to: 72,
  });
});
