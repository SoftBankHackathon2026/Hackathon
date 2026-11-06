import assert from "node:assert/strict";
import test from "node:test";
import type { Control, Posture } from "../src/api/security.ts";
import { defaultFilters } from "../src/features/security/explorer.ts";
import {
  activeTile,
  inboxCounts,
  tilePreset,
} from "../src/features/security/inbox.ts";

function control(
  id: string,
  severity: Control["severity"],
  results: Record<string, string>,
) {
  return {
    id,
    title: id,
    category: "network",
    severity,
    standard_refs: [],
    remediation: "",
    results: Object.fromEntries(
      Object.entries(results).map(([env, status]) => [
        env,
        { status, detail: "" },
      ]),
    ),
  } as Control;
}

const posture = {
  evaluated_at: "",
  source: "live",
  standards: [],
  environments: [],
  controls: [
    control("A", "critical", { src: "pass", dst: "fail" }),
    control("B", "high", { src: "fail", dst: "pass" }),
    control("C", "medium", { src: "unknown", dst: "unknown" }),
  ],
  parity: {
    source_env: "src",
    target_env: "dst",
    regressions: ["A"],
    improvements: ["B"],
  },
} as unknown as Posture;

test("inboxCounts counts controls in the chosen environments", () => {
  assert.deepEqual(inboxCounts(posture, ["src", "dst"]), {
    critical: 1,
    high: 1,
    fail: 2,
    unknown: 1,
    regressed: 1,
    improved: 1,
  });
  assert.equal(inboxCounts(posture, ["src"]).regressed, 0);
});

test("a tile preset becomes the active tile and the default is the fail tile", () => {
  const base = defaultFilters(["src", "dst"], []);
  assert.equal(activeTile(base, posture, ["src", "dst"]), "fail");
  const critical = {
    ...base,
    ...tilePreset("critical", posture, ["src", "dst"]),
  };
  assert.equal(activeTile(critical, posture, ["src", "dst"]), "critical");
  const regressed = {
    ...base,
    ...tilePreset("regressed", posture, ["src", "dst"]),
  };
  assert.deepEqual(regressed.ids, ["A"]);
  assert.equal(activeTile(regressed, posture, ["src", "dst"]), "regressed");
});

test("revealFilters widens only what hides a control", async () => {
  const { revealFilters, matchesFilters } =
    await import("../src/features/security/explorer.ts");
  const withRef = (c: Control) => ({ ...c, standard_refs: ["STD-1 1.1"] });
  const base = defaultFilters(["src", "dst"], ["STD-1"]);
  const failing = withRef(posture.controls[1]);
  assert.equal(revealFilters(base, failing), null);
  const unknownOnly = withRef(posture.controls[2]);
  const patch = revealFilters(base, unknownOnly);
  assert.deepEqual(patch?.status, ["fail", "unknown"]);
  assert.equal(matchesFilters(unknownOnly, { ...base, ...patch }), true);
});
