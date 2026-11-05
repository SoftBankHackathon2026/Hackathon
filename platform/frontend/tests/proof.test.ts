import assert from "node:assert/strict";
import { test } from "node:test";
import type { VerifyReport } from "../src/api/migrations.ts";
import { verifyRows, verifyTotal } from "../src/features/migrations/proof.ts";

function report(
  counts: Partial<VerifyReport["counts"]>,
  passed = true,
): VerifyReport {
  return {
    passed,
    counts: {
      B_minus_T: 0,
      T_minus_B: 0,
      field_mismatch: 0,
      S_minus_T: 0,
      ...counts,
    },
    source_count: 10,
    target_count: 10,
    committed_count: 10,
  };
}

test("verifyTotal adds all four counts and is null without a report", () => {
  assert.equal(verifyTotal(null), null);
  assert.equal(verifyTotal(report({ B_minus_T: 1, S_minus_T: 2 })), 3);
});

test("verifyRows lists the four comparisons in order", () => {
  const rows = verifyRows(report({ field_mismatch: 4 }));
  assert.deepEqual(
    rows.map((r) => r.key),
    ["B_minus_T", "T_minus_B", "field_mismatch", "S_minus_T"],
  );
  assert.equal(rows[2].value, 4);
  assert.ok(verifyRows(null).every((r) => r.value === null));
});
