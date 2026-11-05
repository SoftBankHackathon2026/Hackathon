import assert from "node:assert/strict";
import { test } from "node:test";
import type { SeriesPoint, StageInfo } from "../src/api/migrations.ts";
import {
  bucketTraffic,
  stageMarkers,
} from "../src/features/migrations/traffic.ts";
import { formatEvidence } from "../src/features/migrations/evidence.ts";

function point(i: number, over: Partial<SeriesPoint> = {}): SeriesPoint {
  return {
    at: new Date(Date.UTC(2026, 10, 5, 0, 0, i * 10)).toISOString(),
    committed_source: i * 2,
    committed_target: i,
    write_rate_source: 0,
    write_rate_target: 0,
    queue_length: i,
    latency_p95: 0.1,
    latency_max: i === 3 ? 2 : 0.2,
    failed_total: 0,
    worker_state: "RUNNING",
    stage: "COPYING",
    ...over,
  };
}

test("bucketTraffic turns cumulative counters into per interval deltas", () => {
  const points = Array.from({ length: 9 }, (_, i) => point(i));
  const buckets = bucketTraffic(points, 4);
  assert.equal(buckets.length, 4);
  assert.equal(
    buckets.reduce((s, b) => s + b.source, 0),
    16,
  );
  assert.equal(
    buckets.reduce((s, b) => s + b.target, 0),
    8,
  );
  assert.equal(buckets[0].start, Date.parse(points[0].at));
  assert.equal(buckets[3].end, Date.parse(points[8].at) + 10000);
  assert.ok(buckets.every((b, i) => i === 0 || b.start === buckets[i - 1].end));
});

test("bucketTraffic keeps maxima and null latency", () => {
  const points = [
    point(0),
    point(1),
    point(2, { latency_p95: null, latency_max: null }),
    point(3),
  ];
  const buckets = bucketTraffic(points, 2);
  assert.equal(buckets[1].p95, 0.1);
  assert.equal(buckets[1].max, 2);
  const empty = bucketTraffic(
    [point(0, { latency_p95: null }), point(1, { latency_p95: null })],
    1,
  );
  assert.equal(empty[0].p95, null);
  assert.deepEqual(bucketTraffic([point(0)]), []);
});

test("bucketTraffic never reports negative deltas after a counter reset", () => {
  const points = [
    point(0, { committed_source: 50 }),
    point(1, { committed_source: 5 }),
  ];
  assert.equal(bucketTraffic(points, 1)[0].source, 0);
});

test("stageMarkers lists approval and switch start times that exist", () => {
  const stage = (name: string, started_at: string | null) =>
    ({ name, started_at }) as StageInfo;
  const markers = stageMarkers([
    stage("COPYING", "2026-11-05T00:00:00Z"),
    stage("READY", "2026-11-05T00:01:00Z"),
    stage("SWITCHING", null),
  ]);
  assert.deepEqual(markers, [
    { at: Date.parse("2026-11-05T00:01:00Z"), label: "승인" },
  ]);
  assert.deepEqual(stageMarkers([]), []);
});

test("formatEvidence renders booleans, numbers, lists and objects", () => {
  assert.equal(formatEvidence(true), "예");
  assert.equal(formatEvidence(1000), "1,000");
  assert.equal(formatEvidence(null), "-");
  assert.equal(formatEvidence(["a", 2]), "a, 2");
  assert.equal(formatEvidence({ a: 1 }), '{"a":1}');
});
