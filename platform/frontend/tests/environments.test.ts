import assert from "node:assert/strict";
import { test } from "node:test";
import type { EnvComponent, FleetEnvironment } from "../src/api/migrations.ts";
import {
  NODE_H,
  NODE_W,
  layoutArchitecture,
} from "../src/features/environments/architecture.ts";
import {
  componentTone,
  componentRows,
  detailText,
  envRailRows,
  filterComponentRows,
  dependents,
  envSummary,
  groupByLayer,
  kindLabel,
  nodeDetail,
  registeredRailRow,
} from "../src/features/environments/model.ts";

function component(
  id: string,
  layer: EnvComponent["layer"],
  over: Partial<EnvComponent> = {},
): EnvComponent {
  return {
    id,
    name: id,
    layer,
    kind: "app",
    managed: false,
    status: "healthy",
    detail: "준비 1/1",
    depends_on: [],
    ...over,
  };
}

const components = [
  component("gw", "entry"),
  component("ing", "entry", { depends_on: ["gw"] }),
  component("argo", "platform"),
  component("api", "app", { depends_on: ["ing", "db"] }),
  component("worker", "migration", { depends_on: ["db"] }),
  component("db", "data"),
  component("vpn", "link", { status: "unknown" }),
];

test("envSummary counts statuses and issue severities", () => {
  const summary = envSummary({
    components: [
      component("a", "app"),
      component("b", "app", { status: "unknown" }),
      component("c", "app", { status: "down" }),
    ],
    issues: [
      { severity: "critical", title: "", detail: "" },
      { severity: "warning", title: "", detail: "" },
      { severity: "warning", title: "", detail: "" },
    ],
  });
  assert.deepEqual(summary, {
    total: 3,
    healthy: 1,
    degraded: 0,
    down: 1,
    unknown: 1,
    critical: 1,
    warning: 2,
  });
});

test("groupByLayer follows the fixed layer order and skips empty layers", () => {
  assert.deepEqual(
    groupByLayer(components).map((g) => g.layer),
    ["entry", "platform", "app", "migration", "data", "link"],
  );
  assert.deepEqual(
    groupByLayer([component("a", "data")]).map((g) => g.layer),
    ["data"],
  );
});

test("dependents lists who relies on a component", () => {
  assert.deepEqual(
    dependents(components, "db").map((c) => c.id),
    ["api", "worker"],
  );
});

test("labels and tones are quiet unless something is wrong", () => {
  assert.equal(kindLabel("postgres"), "PostgreSQL");
  assert.equal(kindLabel("custom"), "custom");
  assert.equal(componentTone("healthy"), "ok");
  assert.equal(componentTone("unknown"), "idle");
  assert.equal(componentTone("down"), "bad");
  assert.equal(
    nodeDetail(component("a", "app", { status: "unknown" })),
    "상태 정보 없음",
  );
  assert.equal(nodeDetail(component("a", "app")), "복제본 1개 중 1개 준비됨");
  assert.equal(
    nodeDetail(component("a", "app", { status: "down" })),
    "중단, 복제본 1개 중 1개 준비됨",
  );
});

test("layout puts entry, app and data left to right with bands above and below", () => {
  const layout = layoutArchitecture(components, 900);
  const at = (id: string) => layout.nodes.find((n) => n.id === id)!;
  assert.ok(at("gw").x < at("api").x);
  assert.ok(at("api").x < at("db").x);
  assert.ok(at("db").x < at("vpn").x);
  assert.ok(at("argo").y < at("gw").y);
  assert.ok(at("worker").y > at("db").y);
  assert.ok(layout.width >= 900);
  assert.ok(layout.height >= at("worker").y + NODE_H);
});

test("layout never overlaps nodes", () => {
  const layout = layoutArchitecture(components, 700);
  for (const a of layout.nodes)
    for (const b of layout.nodes) {
      if (a === b) continue;
      const apart =
        a.x + NODE_W <= b.x ||
        b.x + NODE_W <= a.x ||
        a.y + NODE_H <= b.y ||
        b.y + NODE_H <= a.y;
      assert.ok(apart, `${a.id} overlaps ${b.id}`);
    }
});

test("edges run from upstream to downstream and skip unknown ids", () => {
  const layout = layoutArchitecture(
    [...components, component("ghost", "app", { depends_on: ["missing"] })],
    900,
  );
  const pairs = layout.edges.map((e) => `${e.from}>${e.to}`).sort();
  assert.deepEqual(pairs, ["api>db", "gw>ing", "ing>api", "worker>db"]);
  assert.equal(layout.stubs.length, 1);
  assert.equal(layout.stubs[0].id, "vpn");
});

test("layout keeps a minimum width so columns never collide", () => {
  const layout = layoutArchitecture(components, 100);
  assert.ok(layout.width >= 4 * NODE_W);
});

test("detailText turns controller jargon into plain Korean", () => {
  assert.equal(detailText("준비 2/2"), "복제본 2개 중 2개 준비됨");
  assert.equal(detailText("Synced, Healthy"), "정상 동작 중");
  assert.equal(detailText("Unknown, Unknown"), "상태 정보 없음");
  assert.equal(detailText("상태를 읽지 못함"), "상태를 읽지 못했어요");
  assert.equal(
    detailText("관리형 서비스 상태는 아직 수집하지 않음, HTTP 80"),
    "관리형 서비스라 상태를 수집하지 않아요, HTTP 80",
  );
  assert.equal(detailText(""), "");
});

test("envRailRows summarizes an environment in six plain rows", () => {
  const env = {
    id: "onprem",
    name: "온프렘",
    kind: "k3s",
    region: "busan-local",
    observed_at: "2026-11-05T00:00:00Z",
    issues: [],
    components: [
      component("api", "app"),
      component("pg", "data", { kind: "postgres" }),
      component("kf", "data", { kind: "kafka" }),
      component("kf2", "data", { kind: "kafka", status: "unknown" }),
    ],
  } as unknown as FleetEnvironment;
  const rows = envRailRows(env, Date.parse("2026-11-05T00:00:05Z"));
  assert.deepEqual(
    rows.map((r) => r.label),
    ["상태", "구성 요소", "플랫폼", "앱", "데이터", "관측"],
  );
  assert.equal(rows[0].value, "정상 운영 중");
  assert.equal(rows[1].value, "3 / 4 정상");
  assert.equal(rows[2].value, "k3s, busan-local");
  assert.equal(rows[3].value, "1");
  assert.equal(rows[4].value, "PostgreSQL, Kafka");
  assert.equal(rows[5].value, "방금");
});

test("registeredRailRow links a registered environment back to its connection", () => {
  const env = {
    id: "connected-abc_DEF-123",
    kind: "kubernetes",
    registered: true,
    project: "billing",
  } as unknown as FleetEnvironment;
  assert.deepEqual(registeredRailRow(env), {
    label: "출처",
    value: "직접 등록, billing",
    href: "#prepare/abc_DEF-123",
    title: "등록할 때 수집한 정보예요. 준비 화면에서 연결 상태를 볼 수 있어요.",
  });
  assert.equal(
    registeredRailRow({ ...env, project: null })?.value,
    "직접 등록",
  );
  assert.equal(registeredRailRow({ ...env, registered: false }), null);
});

test("componentRows flattens environments and filterComponentRows narrows by env and text", () => {
  const envs = [
    {
      id: "a",
      name: "A",
      provider: "onprem",
      boundary: ["site"],
      components: [component("api", "app", { placement: ["cluster", "ns x"] })],
    },
    {
      id: "b",
      name: "B",
      provider: "aws",
      boundary: ["acct", "region"],
      components: [
        component("rds", "data", { kind: "postgres", placement: [] }),
      ],
    },
  ] as unknown as FleetEnvironment[];
  const rows = componentRows(envs);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].placement, "cluster › ns x");
  assert.equal(rows[1].placement, "region");
  assert.deepEqual(
    filterComponentRows(rows, "b", "").map((r) => r.component.id),
    ["rds"],
  );
  assert.deepEqual(
    filterComponentRows(rows, "all", "NS X").map((r) => r.component.id),
    ["api"],
  );
  assert.equal(filterComponentRows(rows, "all", "zzz").length, 0);
});
