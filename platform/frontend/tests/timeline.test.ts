import assert from "node:assert/strict";
import { test } from "node:test";
import type { SeriesPoint, StageInfo } from "../src/api/migrations.ts";
import {
  firstLastWrite,
  lifetimeMinutes,
  lifetimeSpan,
  lifetimeWindow,
  pointDomain,
  stageAt,
  stageBars,
  writeRuns,
} from "../src/features/migrations/timeline.ts";

const T0 = Date.UTC(2026, 10, 5, 0, 0, 0);

function point(i: number, over: Partial<SeriesPoint> = {}): SeriesPoint {
  return {
    at: new Date(T0 + i * 10000).toISOString(),
    committed_source: 0,
    committed_target: 0,
    write_rate_source: 0,
    write_rate_target: 0,
    queue_length: 0,
    latency_p95: null,
    latency_max: null,
    failed_total: 0,
    worker_state: "RUNNING",
    stage: null,
    ...over,
  };
}

test("pointDomain needs two samples and extends by one step", () => {
  assert.equal(pointDomain([point(0)]), null);
  assert.deepEqual(pointDomain([point(0), point(1), point(2)]), {
    from: T0,
    to: T0 + 30000,
  });
});

test("writeRuns merges samples into ok, wait and fail runs", () => {
  const runs = writeRuns([
    point(0),
    point(1),
    point(2, { worker_state: "PAUSED" }),
    point(3, { worker_state: "SWITCHING" }),
    point(4, { failed_total: 2 }),
    point(5, { failed_total: 2 }),
  ]);
  assert.deepEqual(
    runs.map((r) => [r.state, r.from - T0, r.to - T0]),
    [
      ["ok", 0, 20000],
      ["wait", 20000, 40000],
      ["fail", 40000, 50000],
      ["ok", 50000, 60000],
    ],
  );
  assert.deepEqual(writeRuns([point(0)]), []);
});

function stage(
  name: string,
  from: number | null,
  to: number | null,
): StageInfo {
  return {
    name,
    title: name,
    description: "",
    status: "done",
    started_at: from === null ? null : new Date(T0 + from).toISOString(),
    finished_at: to === null ? null : new Date(T0 + to).toISOString(),
    point_of_no_return: false,
    evidence: {},
  };
}

test("stageBars clips to the window and skips stages outside or unstarted", () => {
  const domain = { from: T0 + 10000, to: T0 + 60000 };
  const bars = stageBars(
    [
      stage("A", 0, 20000),
      stage("B", 20000, null),
      stage("C", null, null),
      stage("D", 90000, 95000),
      stage("E", 0, 5000),
    ],
    domain,
  );
  assert.deepEqual(
    bars.map((b) => [b.name, b.from - T0, b.to - T0]),
    [
      ["A", 10000, 20000],
      ["B", 20000, 60000],
    ],
  );
  assert.equal(stageAt(bars, T0 + 30000)?.name, "B");
  assert.equal(stageAt(bars, T0 + 5000), null);
});

test("firstLastWrite uses the samples where a counter grows", () => {
  const points = [
    point(0, { committed_target: 5 }),
    point(1, { committed_target: 5 }),
    point(2, { committed_target: 8 }),
    point(3, { committed_target: 9 }),
    point(4, { committed_target: 9 }),
  ];
  assert.deepEqual(firstLastWrite(points, "committed_target"), {
    first: T0 + 20000,
    last: T0 + 30000,
  });
  assert.deepEqual(firstLastWrite(points, "committed_source"), {
    first: null,
    last: null,
  });
});

test("stageBars leaves the completed stage to a point marker", () => {
  const bars = stageBars(
    [stage("VERIFYING", 0, 20000), stage("COMPLETED", 20000, null)],
    {
      from: T0,
      to: T0 + 60000,
    },
  );
  assert.deepEqual(
    bars.map((b) => b.name),
    ["VERIFYING"],
  );
});

test("lifetimeSpan runs from the first stage to the completion with padding", () => {
  assert.equal(lifetimeSpan([stage("A", null, null)]), null);
  const running = lifetimeSpan([
    stage("A", 60000, 90000),
    stage("B", 90000, null),
  ]);
  assert.deepEqual(running, { from: T0 + 60000 - 120000, to: null });
  const done = lifetimeSpan([
    stage("A", 0, 30000),
    stage("COMPLETED", 30000, null),
  ]);
  assert.deepEqual(done, { from: T0 - 120000, to: T0 + 30000 + 120000 });
  for (const end of ["ABORTED", "ROLLED_BACK"])
    assert.deepEqual(
      lifetimeSpan([stage("A", 0, 30000), stage(end, 30000, null)]),
      { from: T0 - 120000, to: T0 + 30000 + 120000 },
    );
});

test("lifetimeMinutes pads, rounds to five and caps at a day", () => {
  assert.equal(lifetimeMinutes(T0, T0 + 20 * 60000), 25);
  assert.equal(lifetimeMinutes(T0, T0 + 2 * 60000), 15);
  assert.equal(lifetimeMinutes(T0, T0 + 5000 * 60000), 1440);
});

test("lifetimeWindow keeps only samples inside the job lifetime", () => {
  const points = Array.from({ length: 30 }, (_, i) => point(i));
  const stages = [stage("A", 60000, 120000), stage("COMPLETED", 120000, null)];
  const window = lifetimeWindow(points, stages);
  assert.ok(window);
  assert.equal(
    window.domain.from,
    T0 + 60000 - 120000 < T0 ? T0 : T0 + 60000 - 120000,
  );
  assert.equal(window.domain.to, T0 + 240000);
  assert.ok(window.points.every((p) => Date.parse(p.at) < T0 + 240000));
  assert.equal(window.points.length, 24);
  assert.equal(lifetimeWindow(points, [stage("A", null, null)]), null);
  assert.equal(lifetimeWindow([point(0)], stages), null);
});
