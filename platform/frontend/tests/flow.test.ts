import assert from "node:assert/strict";
import test from "node:test";
import type {
  FleetEnvironment,
  MigrationSummary,
} from "../src/api/migrations.ts";
import {
  attentionItems,
  flowPhases,
  phaseOf,
} from "../src/features/migrations/flow.ts";

function job(over: Partial<MigrationSummary>): MigrationSummary {
  return {
    job_id: "j",
    direction: "forward",
    source_env: "a",
    target_env: "b",
    data_source: "a",
    data_target: "b",
    stage: null,
    status: "pending",
    stage_index: 0,
    stage_count: 10,
    updated_at: null,
    writes: {
      committed_source: 0,
      committed_target: 0,
      failed: 0,
      rejected: 0,
      mismatch: 0,
    },
    downtime: {
      write_pause_seconds: null,
      failed_requests: 0,
      max_latency_seconds: null,
      p95_latency_seconds: null,
    },
    verify_mismatch: null,
    ...over,
  };
}

test("phaseOf maps stages into the six flow phases", () => {
  assert.equal(phaseOf(job({})), "prepare");
  assert.equal(
    phaseOf(job({ status: "running", stage: "catching_up" })),
    "copy",
  );
  assert.equal(
    phaseOf(job({ status: "waiting_approval", stage: "READY" })),
    "approve",
  );
  assert.equal(
    phaseOf(job({ status: "running", stage: "DRAINING" })),
    "switch",
  );
  assert.equal(
    phaseOf(job({ status: "completed", stage: "COMPLETED" })),
    "verify",
  );
  assert.equal(
    phaseOf(job({ status: "needs_recovery", stage: "SWITCHING" })),
    "exception",
  );
});

test("flowPhases places every job exactly once", () => {
  const jobs = [
    job({ job_id: "a" }),
    job({ job_id: "b", status: "running", stage: "COPYING" }),
    job({ job_id: "c", status: "aborted", stage: "ABORTED" }),
  ];
  const { phases, exceptions } = flowPhases(jobs);
  const placed = phases.flatMap((p) => p.jobs.map((j) => j.job_id));
  assert.deepEqual([...placed, ...exceptions.map((j) => j.job_id)].sort(), [
    "a",
    "b",
    "c",
  ]);
  assert.deepEqual(
    phases.find((p) => p.id === "copy")?.jobs.map((j) => j.job_id),
    ["b"],
  );
});

test("attentionItems flags failures and unhealthy apps", () => {
  const env = {
    name: "온프렘",
    apps: [
      { name: "ok-app", health: "Healthy", sync: "Synced" },
      { name: "bad-app", health: "Degraded", sync: "Synced" },
    ],
  } as unknown as FleetEnvironment;
  const items = attentionItems(
    [
      job({
        status: "waiting_approval",
        downtime: { ...job({}).downtime, failed_requests: 3 },
      }),
    ],
    [env],
  );
  const by = Object.fromEntries(items.map((i) => [i.id, i]));
  assert.equal(by.approval.value, 1);
  assert.equal(by.approval.tone, "warn");
  assert.equal(by.failed.value, 3);
  assert.equal(by.failed.tone, "bad");
  assert.equal(by.apps.value, 1);
  assert.equal(by.apps.note, "bad-app");
});

test("phaseIndex places a job in one of the six phases", async () => {
  const { phaseIndex } = await import("../src/features/migrations/flow.ts");
  assert.equal(phaseIndex(job({ status: "running", stage: "COPYING" })), 2);
  assert.equal(
    phaseIndex(job({ status: "needs_recovery", stage: "SWITCHING" })),
    4,
  );
  assert.equal(phaseIndex(job({})), 0);
});

test("flowOverview counts jobs per phase and how far jobs have reached", async () => {
  const { flowOverview } = await import("../src/features/migrations/flow.ts");
  const jobs = [
    job({ job_id: "a" }),
    job({
      job_id: "b",
      status: "running",
      stage: "COPYING",
      writes: {
        ...job({}).writes,
        committed_source: 100,
        committed_target: 50,
      },
    }),
    job({ job_id: "c", status: "completed", stage: "COMPLETED" }),
  ];
  const env = { connected: true } as unknown as FleetEnvironment;
  const cols = flowOverview(jobs, [
    env,
    { connected: false } as unknown as FleetEnvironment,
  ]);
  assert.deepEqual(
    cols.map((c) => c.jobs.map((j) => j.job_id)),
    [["a"], [], ["b"], [], [], ["c"]],
  );
  assert.deepEqual(
    cols.map((c) => c.reached),
    [3, 2, 2, 1, 1, 1],
  );
  assert.equal(cols[0].facts[0].value, "1/2");
  assert.equal(cols[2].facts[1].value, "50.0%");
});
