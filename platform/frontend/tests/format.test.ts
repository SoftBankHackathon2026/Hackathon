import assert from "node:assert/strict";
import { test } from "node:test";
import {
  formatAgo,
  formatRatio,
  formatSeconds,
  imageTag,
  shortRevision,
} from "../src/lib/format.ts";

test("formatSeconds picks a readable unit", () => {
  assert.equal(formatSeconds(0), "0초");
  assert.equal(formatSeconds(0.25), "250ms");
  assert.equal(formatSeconds(2.46), "2.5초");
  assert.equal(formatSeconds(60), "1분");
  assert.equal(formatSeconds(125), "2분 5초");
  assert.equal(formatSeconds(Number.NaN), "-");
});

test("formatAgo buckets elapsed time", () => {
  const now = Date.parse("2026-11-05T12:00:00Z");
  assert.equal(formatAgo("2026-11-05T11:59:55Z", now), "방금");
  assert.equal(formatAgo("2026-11-05T11:59:00Z", now), "1분 전");
  assert.equal(formatAgo("2026-11-05T09:00:00Z", now), "3시간 전");
  assert.equal(formatAgo("bad", now), "-");
});

test("revision and image helpers", () => {
  assert.equal(shortRevision("0123456789abcdef"), "0123456");
  assert.equal(shortRevision("v1"), "v1");
  assert.equal(imageTag("ghcr.io/org/app:1.2"), "app:1.2");
});

test("formatRatio floors so only a full copy reads 100%", () => {
  assert.equal(formatRatio(9996, 10000), "99.9%");
  assert.equal(formatRatio(10000, 10000), "100.0%");
  assert.equal(formatRatio(12000, 10000), "100.0%");
  assert.equal(formatRatio(57, 100), "57.0%");
  assert.equal(formatRatio(1, 3), "33.3%");
  assert.equal(formatRatio(0, 10), "0.0%");
  assert.equal(formatRatio(5, 0), "-");
});
