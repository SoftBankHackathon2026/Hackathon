// 계층 모드(Argo CD 리소스 트리 방식): 환경 → 배치 경로(VPC·서브넷·클러스터·네임스페이스) → 구성 요소.
// 가로 트리로 놓고, 자식이 그룹 하나뿐인 줄은 한 노드로 합친다(예: "EKS 서브넷 / EKS oneaction").
import type {
  ComponentStatus,
  EnvComponent,
  FleetEnvironment,
} from "../../api/migrations.ts";

export type GroupKind =
  "env" | "vpc" | "subnet" | "cluster" | "namespace" | "regional" | "site";

export type TreeNode =
  | {
      type: "group";
      id: string;
      label: string;
      kind: GroupKind;
      children: TreeNode[];
    }
  | { type: "component"; id: string; component: EnvComponent };

const REGIONAL = "리전 서비스";

export function groupKind(label: string): GroupKind {
  if (label.startsWith("ns ")) return "namespace";
  if (label.startsWith("VPC")) return "vpc";
  if (label.includes("서브넷")) return "subnet";
  if (/클러스터|^EKS |^GKE /.test(label)) return "cluster";
  if (label === REGIONAL) return "regional";
  return "site";
}

export function buildTree(env: FleetEnvironment): TreeNode {
  const root: TreeNode & { type: "group" } = {
    type: "group",
    id: env.id,
    label: env.name,
    kind: "env",
    children: [],
  };
  for (const component of env.components) {
    if (component.layer === "link") continue;
    const path =
      component.placement && component.placement.length > 0
        ? component.placement
        : [REGIONAL];
    let node = root;
    path.forEach((label, depth) => {
      const id = `${env.id}/${path.slice(0, depth + 1).join("/")}`;
      let next = node.children.find(
        (c): c is TreeNode & { type: "group" } =>
          c.type === "group" && c.id === id,
      );
      if (!next) {
        next = {
          type: "group",
          id,
          label,
          kind: groupKind(label),
          children: [],
        };
        node.children.push(next);
      }
      node = next;
    });
    node.children.push({
      type: "component",
      id: `${env.id}:${component.id}`,
      component,
    });
  }
  return compress(sortTree(root));
}

function sortTree(node: TreeNode): TreeNode {
  if (node.type === "component") return node;
  const rank = (n: TreeNode) =>
    n.type === "group" ? (n.kind === "regional" ? 2 : 0) : 1;
  return {
    ...node,
    children: node.children
      .map(sortTree)
      .sort(
        (a, b) =>
          rank(a) - rank(b) || labelOf(a).localeCompare(labelOf(b), "ko"),
      ),
  };
}

function compress(node: TreeNode): TreeNode {
  if (node.type === "component") return node;
  let current = node;
  while (
    current.kind !== "env" &&
    current.children.length === 1 &&
    current.children[0].type === "group" &&
    current.children[0].kind !== "namespace"
  ) {
    const only = current.children[0] as TreeNode & { type: "group" };
    current = { ...only, label: `${current.label} / ${only.label}` };
  }
  return { ...current, children: current.children.map(compress) };
}

export function labelOf(node: TreeNode) {
  return node.type === "group" ? node.label : node.component.name;
}

const STATUS_RANK: Record<ComponentStatus, number> = {
  down: 4,
  degraded: 3,
  unknown: 1,
  idle: 0,
  healthy: 2,
};

/** 그룹의 상태: 자손 중 가장 나쁜 것(중단 > 일부 준비 > 정상 > 미수집). */
export function worstStatus(node: TreeNode): ComponentStatus {
  if (node.type === "component") return node.component.status;
  const all = node.children.map(worstStatus);
  if (all.length === 0) return "unknown";
  return all.reduce((a, b) => (STATUS_RANK[b] > STATUS_RANK[a] ? b : a));
}

export function countComponents(node: TreeNode): number {
  return node.type === "component"
    ? 1
    : node.children.reduce((n, c) => n + countComponents(c), 0);
}

export type Placed = {
  node: TreeNode;
  depth: number;
  x: number;
  y: number;
  collapsed: boolean;
};

export const NODE_W = 212;
export const NODE_H = 46;
export const COL_GAP = 40;
export const ROW_GAP = 8;

/** 가로 트리 배치: 잎은 세로로 쌓고, 부모는 첫 자식과 같은 높이(긴 목록도 위에서부터 읽힘). 접힌 그룹은 잎처럼. */
export function layoutTree(root: TreeNode, collapsed: Set<string>) {
  const placed: Placed[] = [];
  const edges: { from: string; to: string }[] = [];
  let cursor = 0;
  const visit = (node: TreeNode, depth: number): number => {
    const x = depth * (NODE_W + COL_GAP);
    const isCollapsed = node.type === "group" && collapsed.has(node.id);
    if (
      node.type === "component" ||
      isCollapsed ||
      node.children.length === 0
    ) {
      const y = cursor;
      cursor += NODE_H + ROW_GAP;
      placed.push({ node, depth, x, y, collapsed: isCollapsed });
      return y;
    }
    const ys = node.children.map((child) => {
      edges.push({ from: node.id, to: child.id });
      return visit(child, depth + 1);
    });
    const y = ys[0];
    placed.push({ node, depth, x, y, collapsed: false });
    return y;
  };
  visit(root, 0);
  const depth = Math.max(...placed.map((p) => p.depth));
  return {
    placed,
    edges,
    width: (depth + 1) * NODE_W + depth * COL_GAP,
    height: Math.max(NODE_H, cursor - ROW_GAP),
  };
}

/** 처음에 접어 둘 그룹: 문제가 없는(모두 정상·미수집) 네임스페이스. 문제 있는 곳만 펼쳐 둔다. */
export function quietNamespaces(root: TreeNode): string[] {
  const out: string[] = [];
  const walk = (node: TreeNode) => {
    if (node.type !== "group") return;
    const status = worstStatus(node);
    if (node.kind === "namespace" && status !== "down" && status !== "degraded")
      out.push(node.id);
    node.children.forEach(walk);
  };
  walk(root);
  return out;
}

/** 노드까지 가는 그룹들의 id(접힌 그룹을 펼칠 때 쓴다). */
export function ancestorIds(root: TreeNode, id: string): string[] {
  const walk = (node: TreeNode, trail: string[]): string[] | null => {
    if (node.id === id) return trail;
    if (node.type !== "group") return null;
    for (const child of node.children) {
      const found = walk(child, [...trail, node.id]);
      if (found) return found;
    }
    return null;
  };
  return walk(root, []) ?? [];
}
