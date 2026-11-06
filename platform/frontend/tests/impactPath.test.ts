import assert from "node:assert/strict";
import test from "node:test";
import type { EnvComponent } from "../src/api/migrations.ts";
import { impactPath } from "../src/features/security/pathModel.ts";

function c(
  id: string,
  layer: string,
  depends_on: string[] = [],
  kind = layer,
): EnvComponent {
  return {
    id,
    name: id,
    layer,
    kind,
    depends_on,
    managed: false,
    status: "healthy",
    detail: "",
  } as EnvComponent;
}

const components = [
  c("lb", "entry", [], "load_balancer"),
  c("web", "app", ["lb", "db"]),
  c("worker", "app", ["web", "queue"]),
  c("db", "data", [], "postgres"),
  c("queue", "data", [], "kafka"),
  c("argocd", "platform"),
];

test("an exposed entry point pulls in the apps behind it and their data", () => {
  const { columns, edges } = impactPath(components, ["lb"]);
  assert.deepEqual(
    columns.map((col) => [col.key, col.nodes.map((n) => n.id)]),
    [
      ["internet", ["internet"]],
      ["entry", ["lb"]],
      ["workload", ["web"]],
      ["data", ["db"]],
    ],
  );
  assert.deepEqual(columns[1].nodes[0].hit, true);
  assert.ok(edges.some((e) => e.from === "web" && e.to === "db"));
});

test("affected workloads show the entry they sit behind and the data they reach", () => {
  const { columns } = impactPath(components, ["web", "worker"]);
  const ids = Object.fromEntries(
    columns.map((col) => [col.key, col.nodes.map((n) => n.id)]),
  );
  assert.deepEqual(ids.entry, ["lb"]);
  assert.deepEqual(ids.workload, ["web", "worker"]);
  assert.deepEqual(ids.data.sort(), ["db", "queue"]);
});

test("no matching resources gives an empty path", () => {
  assert.deepEqual(impactPath(components, ["nothing"]).columns, []);
});
