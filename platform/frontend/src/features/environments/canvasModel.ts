import type {
  ComponentStatus,
  EnvComponent,
  FleetEnvironment,
  MigrationSummary,
} from "../../api/migrations.ts";
import type { Control, Severity } from "../../api/security.ts";
import { LAYER_ORDER } from "./model.ts";

export type ContainerKind =
  "account" | "network" | "subnet" | "cluster" | "namespace";

export type GroupNode = {
  key: string;
  label: string;
  kind: ContainerKind;
  depth: number;
  groups: GroupNode[];
  tiles: EnvComponent[];
};

export function containerKind(label: string): ContainerKind {
  if (
    label.startsWith("사이트") ||
    label.startsWith("AWS 계정") ||
    label.startsWith("GCP 프로젝트")
  )
    return "account";
  if (label.startsWith("리전") || label.startsWith("VPC")) return "network";
  if (label.includes("서브넷")) return "subnet";
  if (label.startsWith("ns ")) return "namespace";
  return "cluster";
}

function sortedTiles(components: EnvComponent[]) {
  return [...components].sort(
    (a, b) => LAYER_ORDER.indexOf(a.layer) - LAYER_ORDER.indexOf(b.layer),
  );
}

export function buildGroups(env: FleetEnvironment): GroupNode {
  const boundary = env.boundary ?? [];
  const rootLabel = boundary[0] ?? env.name;
  const make = (key: string, label: string, depth: number): GroupNode => ({
    key,
    label,
    kind: containerKind(label),
    depth,
    groups: [],
    tiles: [],
  });
  const root = make(env.id, rootLabel, 0);
  for (const component of env.components) {
    const path = [...boundary.slice(1), ...(component.placement ?? [])];
    let node = root;
    for (const label of path) {
      const key = `${node.key}/${label}`;
      let child = node.groups.find((g) => g.key === key);
      if (!child) {
        child = make(key, label, node.depth + 1);
        node.groups.push(child);
      }
      node = child;
    }
    node.tiles.push(component);
  }
  const finish = (node: GroupNode) => {
    node.tiles = sortedTiles(node.tiles);
    node.groups.forEach(finish);
  };
  finish(root);
  return root;
}

export function groupKeyOf(env: FleetEnvironment, component: EnvComponent) {
  const boundary = env.boundary ?? [];
  const path = [...boundary.slice(1), ...(component.placement ?? [])];
  return [env.id, ...path].join("/");
}

export function placementPath(env: FleetEnvironment, component: EnvComponent) {
  return [...(env.boundary ?? []), ...(component.placement ?? [])];
}

const CIDR = /\d{1,3}(?:\.\d{1,3}){3}\/\d{1,2}/;

export function crossLinks(environments: FleetEnvironment[]) {
  const links = environments.map((env) => ({
    env,
    component: env.components.find((c) => c.layer === "link"),
  }));
  const found = links.filter((l) => l.component);
  if (found.length < 2) return [];
  const [a, b] = found;
  const cidr =
    CIDR.exec(a.component?.detail ?? "")?.[0] ??
    CIDR.exec(b.component?.detail ?? "")?.[0] ??
    null;
  return [
    {
      from: { envId: a.env.id, id: (a.component as EnvComponent).id },
      to: { envId: b.env.id, id: (b.component as EnvComponent).id },
      label: cidr ? `Tailscale · ${cidr}` : "Tailscale",
    },
  ];
}

export function statusCounts(components: EnvComponent[]) {
  const count = (status: ComponentStatus) =>
    components.filter((c) => c.status === status).length;
  return {
    healthy: count("healthy"),
    degraded: count("degraded"),
    down: count("down"),
    unknown: count("unknown"),
  };
}

export function problemOrdinals(components: EnvComponent[]) {
  const ordinals = new Map<string, number>();
  components
    .filter((c) => c.status === "degraded" || c.status === "down")
    .forEach((c, i) => ordinals.set(c.id, i + 1));
  return ordinals;
}

export function matchesQuery(
  component: Pick<EnvComponent, "id" | "name" | "kind">,
  query: string,
) {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return `${component.name} ${component.id} ${component.kind}`
    .toLowerCase()
    .includes(q);
}

export type SecurityBadge = { controls: Control[]; severity: Severity };

export type PathNode = { envId: string; id: string };

export function migrationPath(
  job: Pick<
    MigrationSummary,
    "source_env" | "target_env" | "data_source" | "data_target"
  >,
  environments: FleetEnvironment[],
  writeTarget: string | null,
) {
  const env = (id: string) => environments.find((e) => e.id === id);
  const dbOf = (envId: string): PathNode | null => {
    const component = env(envId)?.components.find((c) => c.kind === "postgres");
    return component ? { envId, id: component.id } : null;
  };
  const source = dbOf(job.source_env);
  const target = dbOf(job.target_env);
  const tools: PathNode[] = environments.flatMap((e) =>
    e.components
      .filter((c) => c.layer === "migration" && c.depends_on.length > 0)
      .map((c) => ({ envId: e.id, id: c.id })),
  );
  const worker = tools.find((n) => n.id.includes("worker")) ?? null;
  const connector = tools.find((n) => !n.id.includes("worker")) ?? null;
  const ordered = [source, connector, target].filter(
    (n): n is PathNode => n !== null,
  );
  const linkOf = (envId: string): PathNode | null => {
    const component = env(envId)?.components.find((c) => c.layer === "link");
    return component ? { envId, id: component.id } : null;
  };
  const cdc: PathNode[] = [];
  ordered.forEach((node, i) => {
    const previous = ordered[i - 1];
    if (previous && previous.envId !== node.envId) {
      const from = linkOf(previous.envId);
      const to = linkOf(node.envId);
      if (from) cdc.push(from);
      if (to) cdc.push(to);
    }
    cdc.push(node);
  });
  let writing: PathNode | null = null;
  if (writeTarget !== null) {
    if (writeTarget === job.data_source) writing = source;
    else if (writeTarget === job.data_target) writing = target;
  }
  return {
    cdc,
    worker,
    source,
    target,
    writing,
    write: worker && writing ? [worker, writing] : null,
  };
}
