// 클러스터 모델: 노드 상태, 자원(할당 가능/요청/사용), 노드별 파드. null 은 "읽지 못함"이라 0 과 구분한다.
import type {
  ClusterInfo,
  ClusterNode,
  ClusterResources,
  ComponentPod,
  EnvComponent,
  FleetEnvironment,
} from "../../api/migrations.ts";
import type { Tone } from "../migrations/stages.ts";
import { LAYER_ORDER } from "./model.ts";
import { podLabel, podTone } from "./podModel.ts";

/** 밀리코어 → "1.55코어", "4코어" */
export function cores(millicores: number) {
  const value = millicores / 1000;
  const text =
    value >= 10 ? value.toFixed(1) : value.toFixed(2).replace(/\.?0+$/, "");
  return `${text === "" ? "0" : text}코어`;
}

/** 바이트 → "9.4GiB", "512MiB" */
export function memory(bytes: number) {
  const gib = bytes / 1024 ** 3;
  if (gib >= 1) return `${gib >= 100 ? Math.round(gib) : gib.toFixed(1)}GiB`;
  return `${Math.round(bytes / 1024 ** 2)}MiB`;
}

export type ResourceKey = "cpu" | "memory";

const PICK: Record<ResourceKey, (r: ClusterResources) => number> = {
  cpu: (r) => r.cpu_millicores,
  memory: (r) => r.memory_bytes,
};

export function formatResource(key: ResourceKey, value: number) {
  return key === "cpu" ? cores(value) : memory(value);
}

export type ResourceRow = {
  key: ResourceKey;
  label: string;
  allocatable: number | null;
  requests: number | null;
  usage: number | null;
  /** 할당 가능량 대비 비율(0~). 분모나 분자를 모르면 null */
  usageShare: number | null;
  requestShare: number | null;
  tone: Tone;
};

type Sized = Pick<ClusterNode, "allocatable" | "requests" | "usage">;

function share(part: number | null, whole: number | null) {
  return part === null || whole === null || whole <= 0 ? null : part / whole;
}

/** 실측이 있으면 실측으로, 없으면 요청량으로 붐빔 정도를 판단한다. */
export function loadTone(value: number | null): Tone {
  if (value === null) return "idle";
  if (value >= 0.9) return "bad";
  if (value >= 0.75) return "warn";
  return "ok";
}

export function resourceRows(source: Sized): ResourceRow[] {
  return (["cpu", "memory"] as const).map((key) => {
    const get = (r: ClusterResources | null) => (r ? PICK[key](r) : null);
    const allocatable = get(source.allocatable);
    const requests = get(source.requests);
    const usage = get(source.usage);
    const usageShare = share(usage, allocatable);
    const requestShare = share(requests, allocatable);
    return {
      key,
      label: key === "cpu" ? "CPU" : "메모리",
      allocatable,
      requests,
      usage,
      usageShare,
      requestShare,
      tone: loadTone(usageShare ?? requestShare),
    };
  });
}

export function percent(value: number | null) {
  return value === null ? "모름" : `${Math.round(value * 100)}%`;
}

export function nodeState(node: ClusterNode): { tone: Tone; label: string } {
  if (node.ready === false) return { tone: "bad", label: "준비 안 됨" };
  if (node.unschedulable) return { tone: "warn", label: "배치 중지(cordon)" };
  if (node.ready === null) return { tone: "idle", label: "상태 모름" };
  return { tone: "ok", label: "준비" };
}

export function clusterState(cluster: ClusterInfo | null | undefined): {
  tone: Tone;
  label: string;
} {
  if (!cluster) return { tone: "idle", label: "클러스터 정보 없음" };
  if (cluster.node_count === null)
    return { tone: "idle", label: "노드 못 읽음" };
  if (cluster.ready_nodes === null)
    return { tone: "idle", label: `노드 ${cluster.node_count}개, 상태 모름` };
  const label = `노드 ${cluster.ready_nodes}/${cluster.node_count} 준비`;
  if (cluster.ready_nodes === 0 && cluster.node_count > 0)
    return { tone: "bad", label };
  if (cluster.ready_nodes < cluster.node_count) return { tone: "warn", label };
  return { tone: "ok", label };
}

export function isControlPlane(node: ClusterNode) {
  return node.roles.some((r) => r === "control-plane" || r === "master");
}

export function roleLabel(node: ClusterNode) {
  return isControlPlane(node) ? "컨트롤 플레인" : "워커";
}

/** 화면용 짧은 노드 이름: 도메인을 떼고, 길면 끝부분(관리형 노드 풀은 끝 해시가 구분점)만. */
export function shortNodeName(name: string, max = 20) {
  const host = name.split(".")[0];
  if (host.length <= max) return host;
  const parts = host.split("-");
  const tail = parts.slice(-2).join("-");
  return tail.length + 1 <= max ? `…${tail}` : `…${host.slice(-(max - 1))}`;
}

export type PlacedPod = { pod: ComponentPod; component: EnvComponent };

function byComponent(a: PlacedPod, b: PlacedPod) {
  return (
    LAYER_ORDER.indexOf(a.component.layer) -
      LAYER_ORDER.indexOf(b.component.layer) ||
    a.component.name.localeCompare(b.component.name) ||
    a.pod.name.localeCompare(b.pod.name)
  );
}

/** 노드 이름 → 그 노드의 구성 요소 파드. node 가 없는 파드(아직 배치 안 됨)는 unplaced. */
export function podsByNode(env: FleetEnvironment) {
  const placed = new Map<string, PlacedPod[]>();
  const unplaced: PlacedPod[] = [];
  for (const component of env.components)
    for (const pod of component.pods ?? []) {
      const entry = { pod, component };
      if (!pod.node) unplaced.push(entry);
      else placed.set(pod.node, [...(placed.get(pod.node) ?? []), entry]);
    }
  for (const list of placed.values()) list.sort(byComponent);
  unplaced.sort(byComponent);
  return { placed, unplaced };
}

/** 노드의 파드 중 구성 요소에 속하지 않은 것(시스템 파드 등)의 수. */
export function otherPods(node: ClusterNode, known: number) {
  return node.pods === null ? 0 : Math.max(0, node.pods - known);
}

export type ZoneGroup = { zone: string | null; nodes: ClusterNode[] };

function byRole(a: ClusterNode, b: ClusterNode) {
  return (
    Number(isControlPlane(b)) - Number(isControlPlane(a)) ||
    a.name.localeCompare(b.name)
  );
}

/** 영역(zone)별 노드 묶음. 영역을 모르는 노드는 zone=null 묶음 하나로. */
export function zoneGroups(cluster: ClusterInfo): ZoneGroup[] {
  const zones = new Map<string | null, ClusterNode[]>();
  for (const node of cluster.nodes)
    zones.set(node.zone, [...(zones.get(node.zone) ?? []), node]);
  return [...zones.entries()]
    .sort(([a], [b]) => (a === null ? 1 : b === null ? -1 : a.localeCompare(b)))
    .map(([zone, nodes]) => ({ zone, nodes: [...nodes].sort(byRole) }));
}

export type PodCell = {
  key: string;
  tone: Tone | "other";
  title: string;
  componentId: string | null;
  pod: ComponentPod | null;
};

/** 노드 위에 얹을 파드 칸: 구성 요소별로 붙여 놓고(같은 워크로드끼리 이웃), 나머지는 회색. */
export function nodeCells(
  placed: PlacedPod[],
  others: number,
  max: number,
): { cells: PodCell[]; hidden: number } {
  const cells: PodCell[] = [
    ...placed.map(({ pod, component }) => ({
      key: pod.name,
      tone: podTone(pod),
      title: `${pod.name}\n${component.name}, ${podLabel(pod)}${pod.restarts > 0 ? `, 재시작 ${pod.restarts}` : ""}`,
      componentId: component.id,
      pod,
    })),
    ...Array.from({ length: others }, (_, i) => ({
      key: `other-${i}`,
      tone: "other" as const,
      title: "구성 요소에 속하지 않은 파드(시스템 파드 등)",
      componentId: null,
      pod: null,
    })),
  ];
  return {
    cells: cells.slice(0, max),
    hidden: Math.max(0, cells.length - max),
  };
}

export type ClusterRailRow = {
  label: string;
  value: string;
  tone?: Tone;
  href?: string;
  title?: string;
};

/** 환경 요약(왼쪽 레일)의 클러스터 줄: 노드 준비 수, 자원 사용률. 누르면 노드 보기의 클러스터 패널. */
export function clusterRailRows(env: FleetEnvironment): ClusterRailRow[] {
  const cluster = env.cluster;
  if (cluster === undefined) return [];
  const state = clusterState(cluster);
  const href = `#environments/nodes/${encodeURIComponent(env.id)}`;
  const rows: ClusterRailRow[] = [
    {
      label: "클러스터",
      value: state.label,
      tone: state.tone,
      href,
      title: cluster?.version ?? undefined,
    },
  ];
  if (!cluster) return rows;
  const [cpu, mem] = resourceRows(cluster);
  const measured = cpu.usageShare !== null || mem.usageShare !== null;
  const shareOf = (row: ResourceRow) => row.usageShare ?? row.requestShare;
  if (shareOf(cpu) === null && shareOf(mem) === null) return rows;
  const worst = [cpu, mem].some((r) => r.tone === "bad")
    ? "bad"
    : [cpu, mem].some((r) => r.tone === "warn")
      ? "warn"
      : undefined;
  rows.push({
    label: measured
      ? "사용률"
      : env.observed_namespaces
        ? "범위 내 요청률"
        : "요청률",
    value: `CPU ${percent(shareOf(cpu))}, 메모리 ${percent(shareOf(mem))}`,
    tone: worst,
    title: measured
      ? "할당 가능량 대비 실사용량"
      : env.observed_namespaces
        ? `조회한 네임스페이스의 요청량 ÷ 할당 가능량 (${env.observed_namespaces.join(", ")})`
        : "실사용량을 읽지 못해 할당 가능량 대비 요청량으로 보여 줘요",
  });
  return rows;
}
