import assert from "node:assert/strict";
import test from "node:test";
import type { EnvComponent, FleetEnvironment } from "../src/api/migrations.ts";
import {
  buildTree,
  countComponents,
  labelOf,
  layoutTree,
  NODE_H,
  ROW_GAP,
  worstStatus,
  type TreeNode,
} from "../src/features/environments/treeModel.ts";

function c(
  id: string,
  placement: string[],
  status = "healthy",
  layer = "app",
): EnvComponent {
  return {
    id,
    name: id,
    layer,
    kind: layer,
    depends_on: [],
    managed: false,
    status,
    detail: "",
    placement,
  } as EnvComponent;
}

const env = {
  id: "aws",
  name: "AWS",
  components: [
    c("argocd", ["VPC", "EKS 서브넷", "EKS one", "ns argocd"]),
    c("web", ["VPC", "EKS 서브넷", "EKS one", "ns app"], "down"),
    c("api", ["VPC", "EKS 서브넷", "EKS one", "ns app"]),
    c("rds", ["VPC", "data 서브넷"], "unknown", "data"),
    c("s3", [], "unknown", "data"),
    c("tunnel", ["VPC"], "unknown", "link"),
  ],
} as unknown as FleetEnvironment;

const group = (n: TreeNode) => n as TreeNode & { type: "group" };

test("buildTree nests by placement, merges single-child chains, and drops links", () => {
  const root = group(buildTree(env));
  assert.equal(root.kind, "env");
  assert.deepEqual(root.children.map(labelOf), ["VPC", "리전 서비스"]);
  const vpc = group(root.children[0]);
  assert.deepEqual(
    vpc.children.map(labelOf).sort(),
    ["data 서브넷", "EKS 서브넷 / EKS one"].sort(),
  );
  const cluster = group(
    vpc.children.find((n) => labelOf(n) === "EKS 서브넷 / EKS one")!,
  );
  assert.deepEqual(cluster.children.map(labelOf), ["ns app", "ns argocd"]);
  assert.equal(countComponents(root), 5);
});

test("group status is the worst of its components", () => {
  const root = group(buildTree(env));
  assert.equal(worstStatus(root), "down");
  assert.equal(worstStatus(root.children[1]), "unknown");
});

test("layoutTree stacks leaves and centres parents; collapsed groups act as leaves", () => {
  const root = buildTree(env);
  const open = layoutTree(root, new Set());
  assert.equal(open.height, 5 * (NODE_H + ROW_GAP) - ROW_GAP);
  const vpc = group(group(root).children[0]);
  const cluster = group(
    vpc.children.find((n) => labelOf(n) === "EKS 서브넷 / EKS one")!,
  );
  const folded = layoutTree(root, new Set([cluster.id]));
  assert.equal(folded.height, 3 * (NODE_H + ROW_GAP) - ROW_GAP);
  assert.ok(folded.placed.find((p) => p.node.id === cluster.id)?.collapsed);
});

test("quietNamespaces folds healthy namespaces and keeps troubled ones open", async () => {
  const { quietNamespaces } =
    await import("../src/features/environments/treeModel.ts");
  const ids = quietNamespaces(buildTree(env));
  assert.ok(ids.some((id) => id.endsWith("ns argocd")));
  assert.ok(!ids.some((id) => id.endsWith("ns app")));
});

test("ancestorIds lists the groups above a component", async () => {
  const { ancestorIds } =
    await import("../src/features/environments/treeModel.ts");
  const trail = ancestorIds(buildTree(env), "aws:web");
  assert.equal(trail[0], "aws");
  assert.ok(trail[trail.length - 1].endsWith("ns app"));
});
