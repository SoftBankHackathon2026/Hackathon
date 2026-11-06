import assert from "node:assert/strict";
import test from "node:test";
import type {
  ClusterInfo,
  ClusterNode,
  ComponentPod,
  EnvComponent,
  FleetEnvironment,
} from "../src/api/migrations.ts";
import {
  clusterRailRows,
  clusterState,
  cores,
  memory,
  nodeCells,
  nodeState,
  podsByNode,
  resourceRows,
  shortNodeName,
  zoneGroups,
} from "../src/features/environments/clusterModel.ts";
import { layoutNodes } from "../src/features/environments/nodeLayout.ts";

const GiB = 1024 ** 3;

function node(over: Partial<ClusterNode>): ClusterNode {
  return {
    name: "node-1",
    ready: true,
    unschedulable: false,
    roles: [],
    kubelet_version: "v1.35.0",
    os_image: null,
    arch: "amd64",
    instance_type: "t3.medium",
    zone: null,
    allocatable: { cpu_millicores: 2000, memory_bytes: 4 * GiB },
    requests: { cpu_millicores: 1000, memory_bytes: 2 * GiB },
    usage: null,
    pods: 10,
    ...over,
  };
}

function pod(
  name: string,
  nodeName: string | null,
  ready = true,
): ComponentPod {
  return {
    name,
    phase: ready ? "Running" : "Pending",
    ready,
    restarts: 0,
    node: nodeName,
    started_at: null,
    containers: [],
  };
}

function component(id: string, pods: ComponentPod[]): EnvComponent {
  return {
    id,
    name: id,
    layer: "app",
    kind: "app",
    managed: false,
    status: "healthy",
    detail: "",
    depends_on: [],
    pods,
  };
}

function env(cluster: ClusterInfo | null, components: EnvComponent[] = []) {
  return {
    id: "e",
    name: "e",
    kind: "k3s",
    provider: "onprem",
    region: "r",
    role: "legacy",
    connected: true,
    observed_at: null,
    apps: [],
    data_services: [],
    components,
    issues: [],
    cluster,
  } satisfies FleetEnvironment;
}

function cluster(nodes: ClusterNode[]): ClusterInfo {
  return {
    version: "v1.35.0",
    node_count: nodes.length,
    ready_nodes: nodes.filter((n) => n.ready).length,
    allocatable: null,
    requests: null,
    usage: null,
    nodes,
  };
}

test("cores and memory print short units", () => {
  assert.equal(cores(1548), "1.55코어");
  assert.equal(cores(4000), "4코어");
  assert.equal(cores(250), "0.25코어");
  assert.equal(memory(10115297280), "9.4GiB");
  assert.equal(memory(512 * 1024 ** 2), "512MiB");
});

test("resourceRows uses usage when measured and requests otherwise", () => {
  const [cpu, mem] = resourceRows(
    node({ usage: { cpu_millicores: 1900, memory_bytes: 3 * GiB } }),
  );
  assert.equal(cpu.usageShare, 0.95);
  assert.equal(cpu.requestShare, 0.5);
  assert.equal(cpu.tone, "bad");
  assert.equal(mem.tone, "warn");
  const [requested] = resourceRows(node({}));
  assert.equal(requested.usageShare, null);
  assert.equal(requested.tone, "ok");
  const [unknown] = resourceRows(node({ allocatable: null }));
  assert.equal(unknown.requestShare, null);
  assert.equal(unknown.tone, "idle");
});

test("node and cluster state keep null apart from zero", () => {
  assert.equal(nodeState(node({ ready: false })).tone, "bad");
  assert.equal(nodeState(node({ unschedulable: true })).tone, "warn");
  assert.equal(nodeState(node({ ready: null })).label, "상태 모름");
  assert.equal(clusterState(null).label, "클러스터 정보 없음");
  assert.equal(
    clusterState({ ...cluster([node({})]), node_count: null }).label,
    "노드 못 읽음",
  );
  assert.deepEqual(clusterState(cluster([node({}), node({ ready: false })])), {
    tone: "warn",
    label: "노드 1/2 준비",
  });
});

test("short node names drop the domain and keep the distinguishing tail", () => {
  assert.equal(
    shortNodeName("ip-192-0-2-10.ap-northeast-2.compute.internal"),
    "ip-192-0-2-10",
  );
  assert.equal(
    shortNodeName("gke-demo-cluster-default-pool-232bdfd8-dkj8"),
    "…232bdfd8-dkj8",
  );
});

test("pods group by node, system pods fill the rest in grey", () => {
  const e = env(cluster([node({ name: "a", pods: 4 }), node({ name: "b" })]), [
    component("web", [pod("web-1", "a"), pod("web-2", null, false)]),
    component("api", [pod("api-1", "a")]),
  ]);
  const { placed, unplaced } = podsByNode(e);
  assert.deepEqual(
    placed.get("a")?.map((p) => p.pod.name),
    ["api-1", "web-1"],
  );
  assert.deepEqual(
    unplaced.map((p) => p.pod.name),
    ["web-2"],
  );
  const { cells, hidden } = nodeCells(placed.get("a") ?? [], 2, 3);
  assert.deepEqual(
    cells.map((c) => c.tone),
    ["ok", "ok", "other"],
  );
  assert.equal(hidden, 1);
});

test("zones sort by name with unknown zones last, control plane first", () => {
  const groups = zoneGroups(
    cluster([
      node({ name: "w", zone: "b" }),
      node({ name: "x", zone: null }),
      node({ name: "z", zone: "a" }),
      node({ name: "cp", zone: "a", roles: ["control-plane"] }),
    ]),
  );
  assert.deepEqual(
    groups.map((g) => [g.zone, g.nodes.map((n) => n.name)]),
    [
      ["a", ["cp", "z"]],
      ["b", ["w"]],
      [null, ["x"]],
    ],
  );
});

test("rail rows link to the cluster panel and say which basis they use", () => {
  const rows = clusterRailRows(
    env({
      ...cluster([node({})]),
      allocatable: { cpu_millicores: 2000, memory_bytes: 4 * GiB },
      requests: { cpu_millicores: 1000, memory_bytes: 1 * GiB },
    }),
  );
  assert.equal(rows[0].href, "#environments/nodes/e");
  assert.equal(rows[0].value, "노드 1/1 준비");
  assert.equal(rows[1].label, "요청률");
  assert.equal(rows[1].value, "CPU 50%, 메모리 25%");
  assert.deepEqual(clusterRailRows({ ...env(null), cluster: undefined }), []);
});

test("node layout puts every node on a slab and pods inside it", () => {
  const e = env(
    cluster([
      node({ name: "a", zone: "z1", pods: 3 }),
      node({ name: "b", zone: "z2", pods: 1 }),
    ]),
    [component("web", [pod("web-1", "a"), pod("web-2", null, false)])],
  );
  for (const mode of ["iso", "flat"] as const) {
    const layout = layoutNodes([e, env(null)], mode);
    assert.equal(layout.clusters.length, 2);
    assert.equal(layout.clusters[1].empty, true);
    assert.deepEqual(
      layout.zones.map((z) => z.label),
      ["영역 z1", "영역 z2"],
    );
    assert.deepEqual(
      layout.slabs.map((s) => s.key),
      ["a", "b", "unplaced"],
    );
    for (const slab of layout.slabs)
      for (const cell of slab.cells) {
        assert.ok(cell.x > slab.x && cell.x < slab.x + slab.w, mode);
        assert.ok(cell.y > slab.y && cell.y < slab.y + slab.h, mode);
      }
    assert.equal(layout.slabs[0].cells.length, 3);
  }
});
