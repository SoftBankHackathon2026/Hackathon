import assert from "node:assert/strict";
import test from "node:test";
import type { EnvComponent, FleetEnvironment } from "../src/api/migrations.ts";
import type { Control, Posture } from "../src/api/security.ts";
import {
  categoryTotals,
  exposedEntries,
  severityTotals,
  topFailing,
} from "../src/features/security/overview.ts";

function control(
  id: string,
  severity: Control["severity"],
  category: Control["category"],
  results: Record<string, [string, string[]?]>,
): Control {
  return {
    id,
    title: id,
    category,
    severity,
    standard_refs: [],
    remediation: "",
    results: Object.fromEntries(
      Object.entries(results).map(([env, [status, resources]]) => [
        env,
        { status, detail: "", resources },
      ]),
    ),
  } as Control;
}
function comp(
  id: string,
  layer: string,
  depends_on: string[] = [],
): EnvComponent {
  return {
    id,
    name: id,
    layer,
    kind: layer,
    depends_on,
    managed: false,
    status: "healthy",
    detail: "",
  } as EnvComponent;
}

const controls = [
  control("TLS", "high", "network", { aws: ["fail", ["lb"]], gcp: ["pass"] }),
  control("NETPOL", "high", "network", {
    aws: ["fail", ["web"]],
    gcp: ["fail", []],
  }),
  control("LOG", "medium", "detection", { aws: ["unknown"], gcp: ["fail"] }),
  control("OK", "critical", "data", { aws: ["pass"], gcp: ["pass"] }),
];
const posture = { controls } as unknown as Posture;
const fleet = [
  {
    id: "aws",
    components: [
      comp("lb", "entry"),
      comp("web", "app", ["lb", "db"]),
      comp("db", "data"),
    ],
  },
] as unknown as FleetEnvironment[];

test("severity and category totals count failing controls only", () => {
  assert.deepEqual(
    severityTotals(controls, ["aws", "gcp"]).map((r) => r.count),
    [0, 2, 1, 0],
  );
  const network = categoryTotals(controls, ["aws", "gcp"]).find(
    (r) => r.category === "network",
  );
  assert.deepEqual(network, { category: "network", count: 2, total: 2 });
});

test("topFailing puts more failing environments first within a severity", () => {
  assert.deepEqual(
    topFailing(controls, ["aws", "gcp"]).map((r) => r.control.id),
    ["NETPOL", "TLS", "LOG"],
  );
});

test("exposedEntries groups failing controls by the entry point they reach", () => {
  const [row] = exposedEntries(posture, fleet, ["aws", "gcp"]);
  assert.equal(row.entry.id, "lb");
  assert.deepEqual(row.controls.map((c) => c.id).sort(), ["NETPOL", "TLS"]);
  assert.equal(row.workloads, 1);
  assert.equal(row.data, 1);
  assert.equal(row.worst, "high");
});
