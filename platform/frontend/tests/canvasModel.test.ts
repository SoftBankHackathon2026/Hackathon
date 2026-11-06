import assert from "node:assert/strict";
import { test } from "node:test";
import type { EnvComponent, FleetEnvironment } from "../src/api/migrations.ts";
import type { Control } from "../src/api/security.ts";
import { glyphFor } from "../src/features/environments/glyphs.ts";
import {
  buildGroups,
  containerKind,
  crossLinks,
  groupKeyOf,
  matchesQuery,
  migrationPath,
  placementPath,
  problemOrdinals,
  statusCounts,
} from "../src/features/environments/canvasModel.ts";

function component(
  id: string,
  layer: EnvComponent["layer"],
  kind: string,
  over: Partial<EnvComponent> = {},
): EnvComponent {
  return {
    id,
    name: id,
    layer,
    kind,
    managed: false,
    status: "healthy",
    detail: "",
    depends_on: [],
    placement: [],
    ...over,
  };
}

const onprem = {
  id: "onprem",
  name: "온프렘",
  boundary: ["사이트 busan-local"],
  components: [
    component("ing", "entry", "ingress", {
      placement: ["k3s 클러스터", "ns hackathon"],
    }),
    component("api", "app", "app", {
      placement: ["k3s 클러스터", "ns hackathon"],
    }),
    component("worker", "migration", "migration", {
      placement: ["k3s 클러스터", "ns hackathon"],
      depends_on: ["pg"],
    }),
    component("cdc", "migration", "migration", {
      placement: ["k3s 클러스터", "ns hackathon"],
      depends_on: ["pg"],
    }),
    component("pg", "data", "postgres", {
      placement: ["k3s 클러스터", "ns hackathon"],
    }),
    component("ts-a", "link", "network", {
      detail: "AWS VPC 10.0.0.0/16 경로",
      status: "unknown",
    }),
  ],
} as unknown as FleetEnvironment;

const aws = {
  id: "aws",
  name: "AWS",
  boundary: ["AWS 계정 123", "리전 ap-northeast-2"],
  components: [
    component("alb", "entry", "load_balancer", {
      placement: ["VPC 10.0.0.0/16"],
    }),
    component("svc", "app", "app", {
      placement: ["VPC 10.0.0.0/16", "EKS", "ns platform"],
    }),
    component("rds", "data", "postgres", {
      placement: ["VPC 10.0.0.0/16", "서브넷 db"],
    }),
    component("s3", "data", "object_storage"),
    component("ts-b", "link", "network"),
  ],
} as unknown as FleetEnvironment;

test("containerKind classifies labels by prefix", () => {
  assert.equal(containerKind("사이트 busan-local"), "account");
  assert.equal(containerKind("AWS 계정 1"), "account");
  assert.equal(containerKind("리전 x"), "network");
  assert.equal(containerKind("VPC 10.0.0.0/16"), "network");
  assert.equal(containerKind("서브넷 db"), "subnet");
  assert.equal(containerKind("public 서브넷"), "subnet");
  assert.equal(containerKind("ns platform"), "namespace");
  assert.equal(containerKind("k3s 클러스터"), "cluster");
});

test("buildGroups nests boundary then placement and keeps unplaced tiles at the boundary", () => {
  const root = buildGroups(onprem);
  assert.equal(root.label, "사이트 busan-local");
  assert.deepEqual(
    root.tiles.map((t) => t.id),
    ["ts-a"],
  );
  assert.equal(root.groups[0].label, "k3s 클러스터");
  assert.equal(root.groups[0].groups[0].label, "ns hackathon");
  assert.deepEqual(
    root.groups[0].groups[0].tiles.map((t) => t.id),
    ["ing", "api", "worker", "cdc", "pg"],
  );
  const cloud = buildGroups(aws);
  assert.equal(cloud.label, "AWS 계정 123");
  assert.deepEqual(
    cloud.groups.map((g) => g.label),
    ["리전 ap-northeast-2"],
  );
  const region = cloud.groups[0];
  assert.deepEqual(
    region.tiles.map((t) => t.id),
    ["s3", "ts-b"],
  );
  assert.deepEqual(
    region.groups.map((g) => g.label),
    ["VPC 10.0.0.0/16"],
  );
  assert.deepEqual(region.groups[0].groups.map((g) => g.label).sort(), [
    "EKS",
    "서브넷 db",
  ]);
});

test("buildGroups falls back to a single group named after the environment", () => {
  const bare = {
    ...onprem,
    boundary: undefined,
    components: [component("a", "app", "app")],
  } as unknown as FleetEnvironment;
  const root = buildGroups(bare);
  assert.equal(root.label, "온프렘");
  assert.deepEqual(
    root.tiles.map((t) => t.id),
    ["a"],
  );
});

test("groupKeyOf and placementPath agree with the group tree", () => {
  const api = onprem.components.find((c) => c.id === "api") as EnvComponent;
  assert.equal(groupKeyOf(onprem, api), "onprem/k3s 클러스터/ns hackathon");
  assert.deepEqual(placementPath(onprem, api), [
    "사이트 busan-local",
    "k3s 클러스터",
    "ns hackathon",
  ]);
  const ts = onprem.components.find((c) => c.id === "ts-a") as EnvComponent;
  assert.equal(groupKeyOf(onprem, ts), "onprem");
});

test("crossLinks pairs the two link tiles and labels them with the CIDR", () => {
  const links = crossLinks([onprem, aws]);
  assert.equal(links.length, 1);
  assert.equal(links[0].from.id, "ts-a");
  assert.equal(links[0].to.id, "ts-b");
  assert.equal(links[0].label, "Tailscale · 10.0.0.0/16");
  assert.deepEqual(crossLinks([onprem]), []);
});

test("statusCounts and problemOrdinals", () => {
  const list = [
    component("a", "app", "app"),
    component("b", "app", "app", { status: "down" }),
    component("c", "app", "app", { status: "unknown" }),
    component("d", "app", "app", { status: "degraded" }),
    component("e", "app", "app", { status: "idle" }),
  ];
  assert.deepEqual(statusCounts(list), {
    healthy: 1,
    degraded: 1,
    down: 1,
    idle: 1,
    unknown: 1,
  });
  assert.deepEqual(
    [...problemOrdinals(list)],
    [
      ["b", 1],
      ["d", 2],
    ],
  );
});

test("matchesQuery looks at name, id and kind", () => {
  const c = component("rds", "data", "postgres", { name: "RDS review" });
  assert.equal(matchesQuery(c, ""), true);
  assert.equal(matchesQuery(c, "review"), true);
  assert.equal(matchesQuery(c, "POSTGRES"), true);
  assert.equal(matchesQuery(c, "kafka"), false);
});

test("migrationPath runs source db, tools, target db and inserts the link when crossing", () => {
  const job = {
    source_env: "onprem",
    target_env: "aws",
    data_source: "onprem",
    data_target: "aws",
  };
  const path = migrationPath(job, [onprem, aws], "aws");
  assert.deepEqual(
    path.cdc.map((n) => n.id),
    ["pg", "cdc", "ts-a", "ts-b", "rds"],
  );
  assert.deepEqual(
    path.write?.map((n) => n.id),
    ["worker", "rds"],
  );
  assert.equal(path.writing?.id, "rds");
  const back = migrationPath(
    {
      source_env: "aws",
      target_env: "onprem",
      data_source: "aws",
      data_target: "onprem_return",
    },
    [onprem, aws],
    "aws",
  );
  assert.deepEqual(
    back.cdc.map((n) => n.id),
    ["rds", "ts-b", "ts-a", "cdc", "pg"],
  );
  assert.equal(back.writing?.id, "rds");
  assert.equal(migrationPath(job, [onprem, aws], null).writing, null);
  assert.equal(migrationPath(job, [onprem, aws], null).write, null);
});

test("glyphFor maps kinds and migration tools to distinct glyphs", () => {
  assert.equal(glyphFor({ id: "x", kind: "postgres" }), "database");
  assert.equal(
    glyphFor({ id: "db-insert-worker", kind: "migration" }),
    "worker",
  );
  assert.equal(
    glyphFor({ id: "migration-cdc", kind: "migration" }),
    "connector",
  );
  assert.equal(
    glyphFor({ id: "migration-policy", kind: "migration" }),
    "policy",
  );
  assert.equal(glyphFor({ id: "tail", kind: "network" }), "network");
  assert.equal(glyphFor({ id: "z", kind: "unheard" }), "box");
});

import {
  CELL,
  layoutCanvas,
  pointAlong,
  routeNodes,
  tileCenter,
} from "../src/features/environments/canvasLayout.ts";
import {
  COS,
  SIN,
  blockFaces,
  fitView,
  inkOn,
  layoutBounds,
  project,
  shade,
  shortLabel,
  zoomAt,
} from "../src/features/environments/iso.ts";
import {
  blockPaint,
  containerPaint,
  UNKNOWN_TOP,
} from "../src/features/environments/blockStyle.ts";
import { brandSlug, iconFor } from "../src/features/environments/iconMap.ts";

type Box = { x: number; y: number; w: number; h: number };
const inside = (inner: Box, outer: Box) =>
  inner.x >= outer.x &&
  inner.y >= outer.y &&
  inner.x + inner.w <= outer.x + outer.w &&
  inner.y + inner.h <= outer.y + outer.h;

test("layoutCanvas puts environments side by side with every tile inside its root group", () => {
  const layout = layoutCanvas([onprem, aws]);
  const rootA = layout.groups.find((g) => g.key === "onprem");
  const rootB = layout.groups.find((g) => g.key === "aws");
  assert.ok(rootA && rootB);
  assert.ok(rootA.x + rootA.w < rootB.x);
  assert.equal(layout.tiles.length, 11);
  for (const tile of layout.tiles) {
    const owner = tile.envId === "onprem" ? rootA : rootB;
    assert.ok(inside(tile, owner), tile.id);
  }
  assert.ok(layout.width >= rootB.x + rootB.w);
});

test("layoutCanvas nests child groups inside parents and never overlaps tiles", () => {
  const layout = layoutCanvas([onprem, aws]);
  for (const group of layout.groups) {
    const parent = layout.groups
      .filter((g) => g !== group && group.key.startsWith(`${g.key}/`))
      .sort((a, b) => b.key.length - a.key.length)[0];
    if (parent) assert.ok(inside(group, parent), group.key);
    assert.ok(group.w >= group.label.length * 8);
  }
  for (const a of layout.tiles)
    for (const b of layout.tiles) {
      if (a === b) continue;
      const apart =
        a.x + a.w <= b.x ||
        b.x + b.w <= a.x ||
        a.y + a.h <= b.y ||
        b.y + b.h <= a.y;
      assert.ok(apart, `${a.id} overlaps ${b.id}`);
    }
  assert.ok(layout.tiles.every((t) => t.w === CELL && t.h === CELL));
});

test("link tiles sit on the facing edges at the same height", () => {
  const layout = layoutCanvas([onprem, aws]);
  const a = layout.tiles.find((t) => t.id === "ts-a");
  const b = layout.tiles.find((t) => t.id === "ts-b");
  const rootA = layout.groups.find((g) => g.key === "onprem");
  const vpc = layout.groups.find((g) => g.label.startsWith("VPC"));
  assert.ok(a && b && rootA && vpc);
  assert.equal(a.y, b.y);
  assert.ok(a.x + a.w > rootA.x + rootA.w - 2 * CELL);
  assert.ok(b.x < vpc.x + 2 * CELL);
  assert.ok(a.x < b.x);
});

test("routes and links are straight segments between tile centers", () => {
  const layout = layoutCanvas([onprem, aws]);
  assert.equal(layout.routes.length, 2);
  const route = layout.routes[0];
  const from = layout.tiles.find(
    (t) => t.id === route.from && t.envId === route.envId,
  );
  assert.ok(from);
  assert.deepEqual(route.points[0], tileCenter(from));
  assert.equal(layout.links.length, 1);
  assert.equal(layout.links[0].label, "Tailscale · 10.0.0.0/16");
  assert.deepEqual(
    layout.links[0].labelAt,
    pointAlong(
      layout.links[0].route.points[0],
      layout.links[0].route.points[1],
      0.5,
    ),
  );
});

test("routeNodes connects consecutive path nodes that exist in the layout", () => {
  const layout = layoutCanvas([onprem, aws]);
  const routes = routeNodes(
    [
      { envId: "onprem", id: "pg" },
      { envId: "onprem", id: "ts-a" },
      { envId: "aws", id: "ts-b" },
      { envId: "aws", id: "missing" },
    ],
    layout,
  );
  assert.equal(routes.length, 2);
});

test("project maps floor points to a 2:1 isometric plane and leaves flat mode alone", () => {
  assert.deepEqual(project("flat", 10, 20), [10, 20]);
  const [x, y] = project("iso", 100, 0);
  assert.ok(Math.abs(x - 100 * COS) < 1e-9 && Math.abs(y - 100 * SIN) < 1e-9);
  const [x2, y2] = project("iso", 0, 100);
  assert.ok(Math.abs(x2 + 100 * COS) < 1e-9 && Math.abs(y2 - 100 * SIN) < 1e-9);
});

test("blockFaces builds a lifted top and two side faces in iso, a square in flat", () => {
  const iso = blockFaces("iso", 30, 20);
  assert.equal(iso.top.length, 4);
  assert.equal(iso.left.length, 4);
  assert.equal(iso.right.length, 4);
  assert.deepEqual(iso.topCenter, [0, -20]);
  assert.equal(iso.silhouette.length, 6);
  const flat = blockFaces("flat", 30, 0);
  assert.equal(flat.left.length, 0);
  assert.deepEqual(flat.topCenter, [0, 0]);
});

test("fitView centers the bounds and zoomAt keeps the cursor point fixed", () => {
  const bounds = { minX: 0, maxX: 1000, minY: 0, maxY: 500 };
  const view = fitView(bounds, 800, 400, 0);
  assert.equal(view.k, 0.8);
  assert.equal(view.x, 0);
  assert.equal(view.y, 0);
  const zoomed = zoomAt({ x: 10, y: 20, k: 1 }, 2, 100, 100);
  assert.equal(zoomed.k, 2);
  assert.equal((100 - zoomed.x) / zoomed.k, (100 - 10) / 1);
  assert.equal(zoomAt({ x: 0, y: 0, k: 2.9 }, 10, 0, 0).k, 3);
});

test("layoutBounds covers every projected group corner", () => {
  const layout = layoutCanvas([onprem, aws]);
  const flat = layoutBounds(layout, "flat", 0);
  assert.ok(flat.maxX >= layout.groups[0].x + layout.groups[0].w);
  const iso = layoutBounds(layout, "iso", 0);
  assert.ok(iso.minX < 0);
});

test("shade, inkOn and shortLabel helpers", () => {
  assert.equal(shade("#ffffff", 0.5), "#808080");
  assert.equal(inkOn("#f3f5f9"), "#3a3a3a");
  assert.equal(inkOn("#2b3140"), "#ffffff");
  assert.equal(shortLabel("abcdefghij", 6), "abcde…");
  assert.equal(shortLabel("abc", 6), "abc");
});

test("blockPaint colors by kind and mutes unknown tiles", () => {
  const db = component("pg", "data", "postgres");
  assert.equal(blockPaint(db, false).top, "#0f5c63");
  assert.equal(blockPaint(db, true).top, UNKNOWN_TOP);
  assert.ok(
    blockPaint(db, false).height >
      blockPaint(component("a", "app", "app"), false).height,
  );
});

test("containerPaint follows AWS group conventions", () => {
  assert.equal(
    containerPaint({ kind: "account", label: "AWS 계정 1" }).stroke,
    "#3a3a3a",
  );
  assert.equal(
    containerPaint({ kind: "account", label: "사이트 busan" }).stroke,
    "#8a8a8a",
  );
  assert.equal(
    containerPaint({ kind: "network", label: "리전 x" }).dash,
    "7 5",
  );
  assert.equal(
    containerPaint({ kind: "network", label: "VPC 1" }).stroke,
    "#6a8f7a",
  );
  assert.equal(
    containerPaint({ kind: "subnet", label: "public 서브넷" }).stroke,
    "#6a8f7a",
  );
  assert.equal(
    containerPaint({ kind: "subnet", label: "data 서브넷" }).stroke,
    "#4f7f86",
  );
  assert.equal(
    containerPaint({ kind: "cluster", label: "k3s 클러스터" }).stroke,
    "#6b6b6b",
  );
  assert.equal(
    containerPaint({ kind: "namespace", label: "ns a" }).dash,
    "5 4",
  );
});

test("iconFor prefers brand marks and falls back to line glyphs", () => {
  assert.equal(
    brandSlug({ id: "cloudflared", kind: "cloudflared" }),
    "cloudflare",
  );
  assert.equal(brandSlug({ id: "traefik", kind: "ingress" }), "traefikproxy");
  assert.equal(brandSlug({ id: "argocd", kind: "gitops" }), "argo");
  assert.equal(brandSlug({ id: "argocd-platform", kind: "gitops" }), "argo");
  assert.equal(brandSlug({ id: "argo-rollouts", kind: "rollout" }), "argo");
  assert.equal(brandSlug({ id: "rds", kind: "postgres" }), "postgresql");
  assert.equal(
    brandSlug({ id: "tailscale-aws", kind: "network" }),
    "tailscale",
  );
  assert.equal(brandSlug({ id: "s3", kind: "object_storage" }), null);
  assert.equal(iconFor({ id: "kafka", kind: "kafka" }).filled, true);
  const fallback = iconFor({ id: "s3", kind: "object_storage" });
  assert.equal(fallback.filled, false);
  assert.equal(fallback.slug, "bucket");
});

import {
  cloudProviderOf,
  groupIcon,
  serviceIcon,
} from "../src/features/environments/cloudIcons.ts";

test("serviceIcon maps managed AWS services to official icons and ignores self hosted ones", () => {
  const managed = (kind: string) => ({ kind, managed: true });
  assert.deepEqual(serviceIcon("aws", managed("postgres")), {
    provider: "aws",
    file: "rds",
  });
  assert.deepEqual(serviceIcon("aws", managed("kafka")), {
    provider: "aws",
    file: "msk",
  });
  assert.deepEqual(serviceIcon("aws", managed("object_storage")), {
    provider: "aws",
    file: "s3",
  });
  assert.deepEqual(serviceIcon("aws", managed("load_balancer")), {
    provider: "aws",
    file: "elb",
  });
  assert.deepEqual(serviceIcon("aws", managed("monitoring")), {
    provider: "aws",
    file: "cloudwatch",
  });
  assert.equal(serviceIcon("aws", { kind: "postgres", managed: false }), null);
  assert.equal(serviceIcon(null, managed("postgres")), null);
  assert.equal(serviceIcon("aws", managed("vector_db")), null);
});

test("serviceIcon maps managed GCP services", () => {
  const managed = (kind: string) => ({ kind, managed: true });
  assert.deepEqual(serviceIcon("gcp", managed("postgres")), {
    provider: "gcp",
    file: "cloud-sql",
  });
  assert.deepEqual(serviceIcon("gcp", managed("cache")), {
    provider: "gcp",
    file: "memorystore",
  });
  assert.deepEqual(serviceIcon("gcp", managed("object_storage")), {
    provider: "gcp",
    file: "cloud-storage",
  });
  assert.deepEqual(serviceIcon("gcp", managed("load_balancer")), {
    provider: "gcp",
    file: "load-balancing",
  });
  assert.deepEqual(serviceIcon("gcp", managed("monitoring")), {
    provider: "gcp",
    file: "logging",
  });
  assert.deepEqual(serviceIcon("gcp", managed("serverless")), {
    provider: "gcp",
    file: "cloud-run",
  });
});

test("groupIcon follows AWS group conventions and finds GKE and EKS clusters", () => {
  assert.equal(
    groupIcon("aws", { kind: "account", label: "AWS 계정 1" })?.file,
    "group-cloud",
  );
  assert.equal(
    groupIcon("aws", { kind: "network", label: "리전 ap-northeast-2" })?.file,
    "group-region",
  );
  assert.equal(
    groupIcon("aws", { kind: "network", label: "VPC 10.0.0.0/16" })?.file,
    "group-vpc",
  );
  assert.equal(
    groupIcon("aws", { kind: "subnet", label: "public 서브넷" })?.file,
    "group-public-subnet",
  );
  assert.equal(
    groupIcon("aws", { kind: "subnet", label: "data 서브넷" })?.file,
    "group-private-subnet",
  );
  assert.equal(
    groupIcon("aws", { kind: "cluster", label: "EKS oneaction" })?.file,
    "eks",
  );
  assert.equal(
    groupIcon("gcp", { kind: "cluster", label: "GKE prod" })?.file,
    "gke",
  );
  assert.equal(
    groupIcon(null, { kind: "cluster", label: "k3s 클러스터" }),
    null,
  );
  assert.equal(groupIcon("aws", { kind: "namespace", label: "ns a" }), null);
});

test("cloudProviderOf only recognizes public clouds", () => {
  assert.equal(cloudProviderOf({ provider: "aws" }), "aws");
  assert.equal(cloudProviderOf({ provider: "gcp" }), "gcp");
  assert.equal(cloudProviderOf({ provider: "onprem" }), null);
});
