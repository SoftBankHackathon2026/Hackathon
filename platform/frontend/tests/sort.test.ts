import assert from "node:assert/strict";
import test from "node:test";
import { sortRows } from "../src/lib/sort.ts";

test("sortRows orders numbers and keeps missing values last", () => {
  const rows = [{ v: 2 }, { v: null }, { v: 10 }, { v: 1 }];
  assert.deepEqual(
    sortRows(rows, (r) => r.v, "desc").map((r) => r.v),
    [10, 2, 1, null],
  );
  assert.deepEqual(
    sortRows(rows, (r) => r.v, "asc").map((r) => r.v),
    [1, 2, 10, null],
  );
});

test("sortRows compares text in Korean order", () => {
  const rows = [{ v: "보안" }, { v: "가용성" }, { v: "" }];
  assert.deepEqual(
    sortRows(rows, (r) => r.v, "asc").map((r) => r.v),
    ["가용성", "보안", ""],
  );
});
