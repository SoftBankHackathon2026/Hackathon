import assert from "node:assert/strict";
import { test } from "node:test";
import type { FleetEnvironment } from "../src/api/migrations.ts";
import type { Control } from "../src/api/security.ts";
import {
  buildMarkers,
  groupCounts,
  labelOptions,
  overlayGroupOf,
  overlaps,
  placeFirstFit,
  severityCountsFor,
  textWidth,
} from "../src/features/environments/securityModel.ts";

function control(
  id: string,
  category: Control["category"],
  severity: Control["severity"],
  scope: "resource" | "environment",
  results: Record<
    string,
    { status: "pass" | "fail" | "unknown"; resources?: string[] }
  >,
): Control {
  return {
    id,
    title: `title ${id}`,
    category,
    severity,
    scope,
    standard_refs: [id === "A" ? "CIS-K8S 5.3.2" : "NSA-K8S Network"],
    remediation: "",
    results: Object.fromEntries(
      Object.entries(results).map(([k, v]) => [k, { detail: "", ...v }]),
    ),
  };
}

const env = {
  id: "aws",
  name: "AWS",
  components: [
    { id: "alb", name: "ALB main", layer: "entry", kind: "load_balancer" },
    { id: "lbc", name: "LB Controller", layer: "entry", kind: "ingress" },
    { id: "rds", name: "RDS db", layer: "data", kind: "postgres" },
  ],
} as unknown as FleetEnvironment;

const controls = [
  control("NET-03", "network", "high", "resource", {
    aws: { status: "fail", resources: ["ALB main", "LB Controller"] },
  }),
  control("NET-04", "network", "medium", "resource", {
    aws: { status: "fail", resources: ["ALB main"] },
  }),
  control("ID-01", "identity", "critical", "environment", {
    aws: { status: "fail", resources: [] },
  }),
  control("DATA-01", "data", "high", "resource", {
    aws: { status: "pass", resources: ["RDS db"] },
  }),
  control("ODD-01", "data", "low", "resource", {
    aws: { status: "fail", resources: ["Unknown thing"] },
  }),
];

test("overlayGroupOf folds categories into four overlay groups", () => {
  assert.equal(overlayGroupOf("network"), "misconfig");
  assert.equal(overlayGroupOf("workload"), "misconfig");
  assert.equal(overlayGroupOf("data"), "misconfig");
  assert.equal(overlayGroupOf("identity"), "identity");
  assert.equal(overlayGroupOf("detection"), "detection");
  assert.equal(overlayGroupOf("governance"), "governance");
});

test("buildMarkers puts resource findings on matching tiles and the rest on one boundary marker", () => {
  const markers = buildMarkers(env, controls, { group: null, hidden: [] });
  const alb = markers.get("tile:aws:alb");
  assert.deepEqual(
    alb?.controls.map((c) => c.id),
    ["NET-03", "NET-04"],
  );
  assert.equal(alb?.severity, "high");
  assert.equal(alb?.title, "title NET-03");
  assert.equal(alb?.extra, 1);
  assert.deepEqual(
    markers.get("tile:aws:lbc")?.controls.map((c) => c.id),
    ["NET-03"],
  );
  assert.equal(markers.has("tile:aws:rds"), false);
  const boundary = markers.get("env:aws");
  assert.deepEqual(
    boundary?.controls.map((c) => c.id),
    ["ID-01", "ODD-01"],
  );
  assert.equal(boundary?.severity, "critical");
  assert.equal(boundary?.componentId, null);
});

test("buildMarkers honors the group tab and hidden severities", () => {
  const identity = buildMarkers(env, controls, {
    group: "identity",
    hidden: [],
  });
  assert.deepEqual([...identity.keys()], ["env:aws"]);
  const noHigh = buildMarkers(env, controls, {
    group: "misconfig",
    hidden: ["high"],
  });
  assert.deepEqual(
    noHigh.get("tile:aws:alb")?.controls.map((c) => c.id),
    ["NET-04"],
  );
  assert.equal(noHigh.has("tile:aws:lbc"), false);
});

test("groupCounts and severityCountsFor count failing findings per environment", () => {
  assert.deepEqual(groupCounts([env], controls), {
    misconfig: 3,
    identity: 1,
    detection: 0,
    governance: 0,
  });
  assert.deepEqual(severityCountsFor([env], controls, null), {
    critical: 1,
    high: 1,
    medium: 1,
    low: 1,
  });
  assert.deepEqual(severityCountsFor([env], controls, "identity"), {
    critical: 1,
    high: 0,
    medium: 0,
    low: 0,
  });
});

test("placeFirstFit takes the first free option and falls back to null", () => {
  const options = (x: number) => [
    { key: "a", rect: { x, y: 0, w: 10, h: 10 } },
    { key: "b", rect: { x, y: 20, w: 10, h: 10 } },
  ];
  const result = placeFirstFit(
    [
      { id: "1", options: options(0) },
      { id: "2", options: options(0) },
      { id: "3", options: options(0) },
    ],
    [],
  );
  assert.equal(result.get("1")?.key, "a");
  assert.equal(result.get("2")?.key, "b");
  assert.equal(result.get("3"), null);
  const blocked = placeFirstFit(
    [{ id: "x", options: options(0) }],
    [{ x: 0, y: 0, w: 50, h: 50 }],
  );
  assert.equal(blocked.get("x"), null);
});

test("overlaps, textWidth and labelOptions geometry", () => {
  assert.equal(
    overlaps({ x: 0, y: 0, w: 10, h: 10 }, { x: 5, y: 5, w: 10, h: 10 }),
    true,
  );
  assert.equal(
    overlaps({ x: 0, y: 0, w: 10, h: 10 }, { x: 20, y: 0, w: 10, h: 10 }),
    false,
  );
  assert.ok(textWidth("한글", 10) > textWidth("ab", 10));
  const [right, left, up] = labelOptions(100, 100, 10, 50, 20);
  assert.equal(right.rect.x, 114);
  assert.equal(left.rect.x, 36);
  assert.equal(up.rect.y, 66);
});

import {
  affected,
  defaultFilters,
  excerpt,
  facetCounts,
  filterFindings,
  toggle,
} from "../src/features/security/explorer.ts";

const explorerControls = [
  control("A", "network", "high", "resource", {
    onprem: { status: "fail", resources: ["x", "y"] },
    aws: { status: "fail", resources: ["z"] },
  }),
  control("B", "identity", "critical", "environment", {
    onprem: { status: "fail" },
    aws: { status: "unknown" },
  }),
  control("C", "data", "low", "resource", {
    onprem: { status: "pass", resources: ["db"] },
    aws: { status: "pass", resources: ["db"] },
  }),
];

test("explorer defaults to failing findings sorted by severity", () => {
  const filters = defaultFilters(["onprem", "aws"], ["CIS-K8S", "NSA-K8S"]);
  assert.deepEqual(
    filterFindings(explorerControls, filters).map((c) => c.id),
    ["B", "A"],
  );
  assert.deepEqual(
    filterFindings(explorerControls, { ...filters, status: ["unknown"] }).map(
      (c) => c.id,
    ),
    ["B"],
  );
  assert.deepEqual(
    filterFindings(explorerControls, {
      ...filters,
      env: ["aws"],
      status: ["fail"],
    }).map((c) => c.id),
    ["A"],
  );
});

test("facetCounts ignore their own dimension so users can widen a filter", () => {
  const filters = defaultFilters(["onprem", "aws"], ["CIS-K8S", "NSA-K8S"]);
  const counts = facetCounts(explorerControls, filters);
  assert.deepEqual(counts.severity, {
    critical: 1,
    high: 1,
    medium: 0,
    low: 0,
  });
  assert.equal(counts.status.fail, 2);
  assert.equal(counts.status.unknown, 1);
  assert.equal(counts.status.pass, 1);
  assert.equal(counts.category.network, 1);
  assert.equal(counts.category.data, 0);
  assert.equal(counts.env.onprem, 2);
  assert.equal(counts.env.aws, 1);
});

test("standard filter keeps controls mapped to the chosen standards", () => {
  const filters = defaultFilters(["onprem", "aws"], ["CIS-K8S", "NSA-K8S"]);
  assert.deepEqual(
    filterFindings(explorerControls, { ...filters, standard: ["CIS-K8S"] }).map(
      (c) => c.id,
    ),
    ["A"],
  );
  const counts = facetCounts(explorerControls, {
    ...filters,
    standard: ["CIS-K8S"],
  });
  assert.deepEqual(counts.standard, { "CIS-K8S": 1, "NSA-K8S": 1 });
});

test("affected counts resources and environments for the chosen statuses", () => {
  const filters = defaultFilters(["onprem", "aws"], ["CIS-K8S", "NSA-K8S"]);
  assert.deepEqual(affected(explorerControls[0], filters), {
    envs: ["onprem", "aws"],
    resources: 3,
  });
  assert.deepEqual(affected(explorerControls[1], filters), {
    envs: ["onprem"],
    resources: 0,
  });
});

test("toggle and excerpt helpers", () => {
  assert.deepEqual(toggle(["a"], "b"), ["a", "b"]);
  assert.deepEqual(toggle(["a", "b"], "a"), ["b"]);
  assert.equal(
    excerpt("짧은 문장입니다. 두 번째 문장입니다."),
    "짧은 문장입니다.",
  );
  assert.equal(excerpt("a".repeat(80), 10).length, 10);
  assert.equal(excerpt(undefined), "");
});

test("a marker keeps every failing control at that spot", () => {
  const markers = buildMarkers(env, controls, { group: null, hidden: [] });
  const alb = markers.get("tile:aws:alb");
  assert.ok(alb);
  assert.equal(alb.controls.length, alb.extra + 1);
  assert.deepEqual(
    alb.controls.map((c) => c.id),
    ["NET-03", "NET-04"],
  );
  assert.ok(alb.controls.every((c) => c.results.aws?.status === "fail"));
  const boundary = markers.get("env:aws");
  assert.deepEqual(
    boundary?.controls.map((c) => c.severity),
    ["critical", "low"],
  );
});
