import assert from "node:assert/strict";
import { test } from "node:test";
import type { MigrationSummary, StageInfo } from "../src/api/migrations.ts";
import { buildLanes } from "../src/features/migrations/lanes.ts";
import {
  filterJobs,
  overviewCards,
  splitUnit,
} from "../src/features/migrations/summary.ts";
import {
  BAND_LABELS,
  complianceBar,
  scoreBand,
} from "../src/features/security/posture.ts";

const T0 = Date.UTC(2026, 10, 5, 0, 0, 0);

function job(
  id: string,
  over: Partial<MigrationSummary> = {},
  failed = 0,
  pause: number | null = 0,
  mismatch: number | null = 0,
) {
  return {
    job_id: id,
    source_env: "onprem",
    target_env: "aws",
    status: "completed",
    writes: { mismatch: 0, rejected: 0 },
    downtime: { failed_requests: failed, write_pause_seconds: pause },
    verify_mismatch: mismatch,
    ...over,
  } as unknown as MigrationSummary;
}

function stage(
  name: string,
  from: number | null,
  to: number | null,
  status: StageInfo["status"] = "done",
): StageInfo {
  return {
    name,
    title: name,
    description: "",
    status,
    started_at: from === null ? null : new Date(T0 + from).toISOString(),
    finished_at: to === null ? null : new Date(T0 + to).toISOString(),
    point_of_no_return: false,
    evidence: {},
  };
}

test("splitUnit separates the number from its unit", () => {
  assert.deepEqual(splitUnit("30초"), { value: "30", unit: "초" });
  assert.deepEqual(splitUnit("1,200건"), { value: "1,200", unit: "건" });
  assert.deepEqual(splitUnit("-"), { value: "-", unit: "" });
});

test("overviewCards state failures, longest pause and verification in plain units", () => {
  const cards = overviewCards([
    job("a", {}, 0, 30, 0),
    job("b", { verify_mismatch: null }, 0, 0, null),
  ]);
  assert.equal(cards[0].label?.text, "무중단");
  assert.equal(cards[0].value, "0");
  assert.deepEqual(cards[0].lines, ["a 0건", "b 0건"]);
  assert.equal(cards[1].value, "30");
  assert.equal(cards[1].unit, "초");
  assert.deepEqual(cards[1].lines, ["a, FENCING–SWITCHING"]);
  assert.equal(cards[2].label, null);
  assert.deepEqual(cards[2].lines, ["검증 2건 중 1건 완료"]);
  const bad = overviewCards([job("a", {}, 2, 5, 3)]);
  assert.equal(bad[0].label?.text, "실패 있음");
  assert.equal(bad[2].label?.tone, "bad");
  assert.equal(overviewCards([])[0].label, null);
});

test("filterJobs narrows by environment and status", () => {
  const jobs = [
    job("a"),
    job("b", {
      status: "waiting_approval",
      source_env: "aws",
      target_env: "gcp",
    }),
  ];
  assert.equal(filterJobs(jobs, "all", "all").length, 2);
  assert.deepEqual(
    filterJobs(jobs, "gcp", "all").map((j) => j.job_id),
    ["b"],
  );
  assert.deepEqual(
    filterJobs(jobs, "all", "waiting").map((j) => j.job_id),
    ["b"],
  );
  assert.deepEqual(
    filterJobs(jobs, "all", "done").map((j) => j.job_id),
    ["a"],
  );
  assert.deepEqual(
    filterJobs(jobs, "all", "active").map((j) => j.job_id),
    ["b"],
  );
});

test("buildLanes shares one time axis and marks the write pause", () => {
  const lanes = buildLanes(
    [
      {
        jobId: "a",
        stages: [
          stage("COPYING", 0, 60000),
          stage("FENCING", 60000, 90000),
          stage("VERIFYING", 90000, 120000, "failed"),
          stage("COMPLETED", 120000, null),
        ],
      },
      { jobId: "b", stages: [stage("COPYING", 300000, null, "current")] },
      { jobId: "c", stages: [stage("READY", null, null, "pending")] },
    ],
    T0 + 400000,
  );
  assert.ok(lanes);
  assert.deepEqual(
    lanes.lanes[0].bars.map((b) => b.kind),
    ["done", "pause", "failed"],
  );
  assert.equal(lanes.lanes[1].bars[0].kind, "current");
  assert.equal(lanes.lanes[1].bars[0].to, T0 + 400000);
  assert.equal(lanes.lanes[2].bars.length, 0);
  assert.equal(lanes.ticks.length, 6);
  assert.ok(lanes.domain.from < T0);
  assert.ok(lanes.domain.to > T0 + 400000);
  assert.equal(buildLanes([{ jobId: "x", stages: [] }], T0), null);
});

test("scoreBand and complianceBar", () => {
  assert.equal(scoreBand(80), "high");
  assert.equal(scoreBand(57), "mid");
  assert.equal(scoreBand(49), "low");
  assert.equal(BAND_LABELS.low, "기준 미달");
  const bar = complianceBar({ passed: 10, failed: 10, unknown: 3 }, 23);
  assert.equal(bar.total, 23);
  assert.deepEqual(
    bar.segments.map((s) => [s.key, s.count, s.percent]),
    [
      ["pass", 10, 43],
      ["fail", 10, 43],
      ["unknown", 3, 13],
    ],
  );
  assert.ok(Math.abs(bar.segments[0].width - (10 / 23) * 100) < 1e-9);
  assert.equal(
    complianceBar({ passed: 0, failed: 0, unknown: 0 }, 0).segments[0].percent,
    0,
  );
});
