import assert from "node:assert/strict";
import { test } from "node:test";
import type { Control } from "../src/api/security.ts";
import {
  filterControls,
  compareSummary,
  groupByCategory,
  hasEnoughEvidence,
  NO_FILTERS,
  overviewScore,
  scoreTone,
  standardCompliance,
  standardId,
} from "../src/features/security/posture.ts";

function control(
  id: string,
  category: Control["category"],
  severity: Control["severity"],
  statuses: Record<string, Control["results"][string]["status"]>,
  standards: string[] = [],
): Control {
  return {
    id,
    title: id,
    category,
    severity,
    standard_refs: standards,
    remediation: "",
    results: Object.fromEntries(
      Object.entries(statuses).map(([env, status]) => [
        env,
        { status, detail: "" },
      ]),
    ),
  };
}

const controls = [
  control("a", "network", "low", { k3s: "pass", eks: "fail" }, ["cis"]),
  control("b", "network", "critical", { k3s: "pass", eks: "pass" }),
  control("c", "data", "high", { k3s: "unknown", eks: "not_applicable" }, [
    "cis",
    "nist",
  ]),
];

test("no filters keeps every control", () => {
  assert.equal(filterControls(controls, NO_FILTERS).length, 3);
});

test("filters combine with AND", () => {
  const ids = (f: Partial<typeof NO_FILTERS>) =>
    filterControls(controls, { ...NO_FILTERS, ...f }).map((c) => c.id);
  assert.deepEqual(ids({ severity: "critical" }), ["b"]);
  assert.deepEqual(ids({ category: "network" }), ["a", "b"]);
  assert.deepEqual(ids({ status: "fail" }), ["a"]);
  assert.deepEqual(ids({ status: "unknown" }), ["c"]);
  assert.deepEqual(ids({ query: " B " }), ["b"]);
  assert.deepEqual(ids({ query: "b", category: "data" }), []);
});

test("groupByCategory orders categories and sorts by severity", () => {
  const groups = groupByCategory(controls);
  assert.deepEqual(
    groups.map((g) => g.category),
    ["network", "data"],
  );
  assert.deepEqual(
    groups[0].controls.map((c) => c.id),
    ["b", "a"],
  );
  assert.deepEqual(groupByCategory([]), []);
});

test("scoreTone thresholds", () => {
  assert.equal(scoreTone(95), "ok");
  assert.equal(scoreTone(80), "ok");
  assert.equal(scoreTone(79), "warn");
  assert.equal(scoreTone(49), "bad");
});

test("standardId reads the prefix of a reference", () => {
  assert.equal(standardId("CIS-K8S 5.3.2"), "CIS-K8S");
  assert.equal(standardId("NSA-K8S"), "NSA-K8S");
});

test("standardCompliance scores each standard per environment", () => {
  const rows = standardCompliance(
    [
      { id: "cis", name: "CIS" },
      { id: "none", name: "Empty" },
    ],
    controls,
    ["k3s", "eks"],
  );
  const cis = rows[0];
  assert.equal(cis.members, 2);
  assert.deepEqual(cis.envs.k3s, {
    pass: 1,
    fail: 0,
    unknown: 1,
    total: 2,
    score: 100,
    coverage: 0.5,
    failedBySeverity: { critical: 0, high: 0, medium: 0, low: 0 },
  });
  assert.equal(cis.envs.eks.fail, 1);
  assert.equal(cis.envs.eks.failedBySeverity.low, 1);
  assert.equal(cis.envs.eks.total, 1);
  assert.equal(cis.envs.eks.unknown, 0);
  assert.equal(rows[1].envs.k3s.score, null);
  assert.equal(rows[1].envs.k3s.coverage, null);
});

test("hasEnoughEvidence needs at least half of the checks to be known", () => {
  assert.equal(hasEnoughEvidence(5, 10), true);
  assert.equal(hasEnoughEvidence(4, 10), false);
  assert.equal(hasEnoughEvidence(0, 0), false);
});

test("overviewScore hides the score when too little was checked", () => {
  assert.deepEqual(
    overviewScore({ passed: 10, failed: 10, unknown: 3, score: 57 }),
    {
      total: 23,
      score: 57,
      passShare: 43,
    },
  );
  assert.equal(
    overviewScore({ passed: 1, failed: 0, unknown: 7, score: 100 }).score,
    null,
  );
  assert.equal(
    overviewScore({ passed: 0, failed: 0, unknown: 0, score: 0 }).passShare,
    0,
  );
});

test("compareSummary explains the migration in a sentence and names strengthened controls", () => {
  const list = [
    control("A", "network", "low", {}),
    control("B", "network", "low", {}),
  ];
  list[0].title = "첫째";
  list[1].title = "둘째";
  const clean = compareSummary(
    { regressions: [], improvements: ["A", "B", "X", "Y"] },
    list,
  );
  assert.equal(clean.sentence, "도착 환경에서 새로 실패한 통제는 없어요");
  assert.deepEqual(clean.strengthened, ["첫째", "둘째", "X"]);
  assert.equal(clean.moreStrengthened, 1);
  const weak = compareSummary({ regressions: ["B"], improvements: [] }, list);
  assert.equal(weak.sentence, "도착 환경에서 새로 실패한 통제가 1개 있어요");
  assert.deepEqual(weak.weakened, ["둘째"]);
});
