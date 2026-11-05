import assert from "node:assert/strict";
import { test } from "node:test";
import type { SeriesPoint } from "../src/api/migrations.ts";
import {
  firstStageTime,
  pauseWindow,
  peak,
  stageSpans,
} from "../src/features/migrations/series.ts";

function point(second: number, stage: string): SeriesPoint {
  return {
    at: new Date(Date.UTC(2026, 10, 5, 0, 0, second)).toISOString(),
    committed_source: 0,
    committed_target: 0,
    write_rate_source: 0,
    write_rate_target: 0,
    queue_length: 0,
    latency_p95: 0,
    latency_max: 0,
    failed_total: 0,
    worker_state: "RUNNING",
    stage,
  };
}

const ms = (second: number) => Date.UTC(2026, 10, 5, 0, 0, second);
const points = [
  point(0, "COPYING"),
  point(10, "READY"),
  point(20, "FENCING"),
  point(30, "DRAINING"),
  point(40, "SWITCHING"),
  point(50, "VERIFYING"),
  point(60, "COMPLETED"),
];

test("stageSpans groups consecutive stages and stays contiguous", () => {
  const spans = stageSpans(points);
  assert.equal(spans.length, 7);
  assert.deepEqual(spans[0], { stage: "COPYING", from: ms(0), to: ms(10) });
  assert.equal(spans[6].to, ms(60));
  const merged = stageSpans([
    point(0, "ready"),
    point(5, "READY"),
    point(9, "FENCING"),
  ]);
  assert.equal(merged.length, 2);
  assert.deepEqual(merged[0], { stage: "READY", from: ms(0), to: ms(9) });
});

test("pauseWindow runs from the first paused point until the next point after the last", () => {
  assert.deepEqual(pauseWindow(points), { from: ms(20), to: ms(50) });
  assert.equal(pauseWindow([point(0, "COPYING"), point(10, "READY")]), null);
  assert.deepEqual(pauseWindow([point(0, "READY"), point(10, "FENCING")]), {
    from: ms(10),
    to: ms(10),
  });
});

test("firstStageTime and peak", () => {
  assert.equal(firstStageTime(points, "switching"), ms(40));
  assert.equal(firstStageTime(points, "ABORTED"), null);
  assert.equal(peak([1, 7, 3]), 7);
  assert.equal(peak([]), 0);
});
