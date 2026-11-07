import assert from "node:assert/strict";
import { test } from "node:test";
import {
  bucketPosition,
  clampIndex,
  compactCount,
  linePath,
  nearestIndex,
  niceAxis,
  scaleLinear,
  spreadIndexes,
  stackValues,
} from "../src/components/charts/chartMath.ts";

test("niceAxis gives four even steps and never drops below the floor", () => {
  assert.deepEqual(niceAxis(0).ticks, [0, 1, 2, 3, 4]);
  assert.deepEqual(niceAxis(3).ticks, [0, 1, 2, 3, 4]);
  assert.deepEqual(niceAxis(29.21, 1).ticks, [0, 8, 16, 24, 32]);
  assert.deepEqual(niceAxis(0.04, 0.04).ticks, [0, 0.01, 0.02, 0.03, 0.04]);
  assert.equal(niceAxis(Number.NaN).top, 4);
});

test("compactCount shortens thousands and millions", () => {
  assert.equal(compactCount(7), "7");
  assert.equal(compactCount(2500), "2.5k");
  assert.equal(compactCount(1_200_000), "1.2M");
});

test("spreadIndexes returns at most the requested count, evenly", () => {
  assert.deepEqual(spreadIndexes(3), [0, 1, 2]);
  assert.deepEqual(spreadIndexes(61, 7), [0, 10, 20, 30, 40, 50, 60]);
});

test("bucketPosition places a time inside a bucket", () => {
  const buckets = [
    { start: 0, end: 10 },
    { start: 10, end: 20 },
  ];
  assert.equal(bucketPosition(buckets, -5), 0);
  assert.equal(bucketPosition(buckets, 15), 1.5);
  assert.equal(bucketPosition(buckets, 99), 2);
  assert.equal(bucketPosition([], 1), 0);
});

test("scaleLinear maps domain to range and survives a flat domain", () => {
  const scale = scaleLinear([0, 10], [100, 200]);
  assert.equal(scale(5), 150);
  assert.equal(scaleLinear([3, 3], [0, 10])(3), 5);
});

test("linePath breaks at gaps", () => {
  assert.equal(linePath([0, 1, 2], [1, Number.NaN, 3]), "M0 1 M2 3");
});

test("stackValues accumulates layers and ignores negatives", () => {
  const [a, b] = stackValues([
    [1, 2],
    [3, -4],
  ]);
  assert.deepEqual(a.top, [1, 2]);
  assert.deepEqual(b.bottom, [1, 2]);
  assert.deepEqual(b.top, [4, 2]);
});

test("nearestIndex picks the closest sample", () => {
  assert.equal(nearestIndex([], 5), -1);
  assert.equal(nearestIndex([0, 10, 20], 4), 0);
  assert.equal(nearestIndex([0, 10, 20], 16), 2);
});

test("clampIndex keeps a stale hover inside a shrunk series", () => {
  assert.equal(clampIndex(null, 5), null);
  assert.equal(clampIndex(3, 0), null);
  assert.equal(clampIndex(9, 4), 3);
  assert.equal(clampIndex(2, 4), 2);
  assert.equal(clampIndex(-1, 4), 0);
});
