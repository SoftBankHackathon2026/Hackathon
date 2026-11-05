import assert from "node:assert/strict";
import { test } from "node:test";
import { barPercent } from "../src/lib/bar.ts";

test("barPercent scales to the column maximum and hides zeros", () => {
  assert.equal(barPercent(50, 100), 50);
  assert.equal(barPercent(0, 100), 0);
  assert.equal(barPercent(5, 0), 0);
  assert.equal(barPercent(1, 1000), 3);
  assert.equal(barPercent(500, 100), 100);
  assert.equal(barPercent(Number.NaN, 100), 0);
});
