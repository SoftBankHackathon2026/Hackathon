import assert from "node:assert/strict";
import { test } from "node:test";
import { severityLevel } from "../src/features/security/severityBars.ts";

test("severityLevel fills one to three cells", () => {
  assert.equal(severityLevel("low"), 1);
  assert.equal(severityLevel("medium"), 2);
  assert.equal(severityLevel("high"), 3);
  assert.equal(severityLevel("critical"), 3);
});
