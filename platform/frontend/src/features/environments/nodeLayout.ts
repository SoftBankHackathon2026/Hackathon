// 노드 보기 배치(바닥 좌표): 환경 → 클러스터 판 → 영역 판 → 노드 판 → 파드 칸.
// Cloudcraft 쿠버네티스 그림처럼 노드를 판으로 놓고 그 위에 파드를 하나씩 얹는다.
import type { ClusterNode, FleetEnvironment } from "../../api/migrations.ts";
import { COS, SIN } from "./iso.ts";
import {
  nodeCells,
  otherPods,
  podsByNode,
  zoneGroups,
  type PodCell,
} from "./clusterModel.ts";

export const POD_STEP = 16;
export const POD_COLS = 7;
export const MAX_CELLS = 60;
export const NODE_PAD = 12;
// 머리 줄(이름, CPU/메모리 막대). 뒷줄 파드 블록이 iso 에서 위로 솟아도 막대를 가리지 않을 만큼 띄운다.
export const NODE_HEAD = 64;
export const NODE_GAP = 22;
export const ZONE_PAD = 14;
export const ZONE_HEAD = 36;
export const CLUSTER_PAD = 16;
export const CLUSTER_HEAD = 46;
export const ENV_HEAD = 48;
const EMPTY_W = 300;
const EMPTY_H = 120;

export type NodeSlab = {
  envId: string;
  /** null = 아직 노드에 배치되지 않은 파드 묶음 */
  node: ClusterNode | null;
  key: string;
  x: number;
  y: number;
  w: number;
  h: number;
  cells: (PodCell & { x: number; y: number })[];
  hidden: number;
};
export type Plate = {
  envId: string;
  key: string;
  label: string;
  x: number;
  y: number;
  w: number;
  h: number;
};
export type NodeLayout = {
  width: number;
  height: number;
  envHeads: { envId: string; x: number; y: number; w: number }[];
  clusters: (Plate & { empty: boolean })[];
  zones: Plate[];
  slabs: NodeSlab[];
};

type Item = {
  node: ClusterNode | null;
  key: string;
  cells: PodCell[];
  hidden: number;
};

function slabSize(rows: number) {
  return {
    w: POD_COLS * POD_STEP + NODE_PAD * 2,
    h: NODE_HEAD + Math.max(1, rows) * POD_STEP + NODE_PAD,
  };
}

function placeCells(cells: PodCell[], x: number, y: number) {
  return cells.map((cell, i) => ({
    ...cell,
    x: x + NODE_PAD + POD_STEP / 2 + (i % POD_COLS) * POD_STEP,
    y: y + NODE_HEAD + POD_STEP / 2 + Math.floor(i / POD_COLS) * POD_STEP,
  }));
}

type EnvBlock = Omit<NodeLayout, "width" | "height"> & {
  w: number;
  h: number;
};

/** 환경 하나를 원점(0, 0) 기준으로 배치: 맨 위 환경 이름 줄, 그 아래 클러스터 판. */
function layoutEnv(env: FleetEnvironment): EnvBlock {
  const out: EnvBlock = {
    w: 0,
    h: 0,
    envHeads: [],
    clusters: [],
    zones: [],
    slabs: [],
  };
  const top = ENV_HEAD;
  const cluster = env.cluster;
  const clusterLabel = env.boundary?.find(
    (label) =>
      !/^(사이트|AWS 계정|GCP 프로젝트|리전|VPC|ns )/.test(label) &&
      !label.includes("서브넷"),
  );
  const label = clusterLabel ?? `${env.kind.toUpperCase()} 클러스터`;
  if (!cluster || cluster.nodes.length === 0) {
    out.envHeads.push({ envId: env.id, x: 0, y: 0, w: EMPTY_W });
    out.clusters.push({
      envId: env.id,
      key: `${env.id}/cluster`,
      label,
      x: 0,
      y: top,
      w: EMPTY_W,
      h: EMPTY_H,
      empty: true,
    });
    return { ...out, w: EMPTY_W, h: top + EMPTY_H };
  }
  const { placed, unplaced } = podsByNode(env);
  const itemOf = (node: ClusterNode): Item => {
    const mine = placed.get(node.name) ?? [];
    const { cells, hidden } = nodeCells(
      mine,
      otherPods(node, mine.length),
      MAX_CELLS,
    );
    return { node, key: node.name, cells, hidden };
  };
  const groups = zoneGroups(cluster).map((g) => ({
    zone: g.zone,
    items: g.nodes.map(itemOf),
  }));
  if (unplaced.length > 0) {
    const { cells, hidden } = nodeCells(unplaced, 0, MAX_CELLS);
    groups.push({
      zone: null,
      items: [{ node: null, key: "unplaced", cells, hidden }],
    });
  }
  // 노드 판은 한 클러스터 안에서 같은 크기(가장 많은 파드에 맞춤)로 놓아 나란히 비교되게 한다.
  const rows = Math.max(
    1,
    ...groups.flatMap((g) =>
      g.items.map((item) => Math.ceil(item.cells.length / POD_COLS)),
    ),
  );
  const slab = slabSize(rows);
  const zoned = groups.some((g) => g.zone !== null);
  // 영역이 있으면 영역마다 세로 열(AWS 가용 영역 그림처럼), 없으면 노드를 가로로 나란히.
  const y0 = top + CLUSTER_HEAD;
  let x = CLUSTER_PAD;
  let bottom = y0;
  for (const group of groups) {
    const framed = zoned && group.zone !== null;
    const pad = framed ? ZONE_PAD : 0;
    const head = framed ? ZONE_HEAD : 0;
    const across = !zoned;
    group.items.forEach((item, i) => {
      const sx = x + pad + (across ? i * (slab.w + NODE_GAP) : 0);
      const sy = y0 + head + pad + (across ? 0 : i * (slab.h + NODE_GAP));
      out.slabs.push({
        envId: env.id,
        node: item.node,
        key: item.key,
        x: sx,
        y: sy,
        w: slab.w,
        h: slab.h,
        cells: placeCells(item.cells, sx, sy),
        hidden: item.hidden,
      });
    });
    const n = group.items.length;
    const groupW =
      (across ? n * slab.w + (n - 1) * NODE_GAP : slab.w) + pad * 2;
    const groupH =
      (across ? slab.h : n * slab.h + (n - 1) * NODE_GAP) + head + pad * 2;
    if (framed)
      out.zones.push({
        envId: env.id,
        key: `${env.id}/zone/${group.zone}`,
        label: `영역 ${group.zone}`,
        x,
        y: y0,
        w: groupW,
        h: groupH,
      });
    x += groupW + NODE_GAP;
    bottom = Math.max(bottom, y0 + groupH);
  }
  // 영역 열은 높이를 맞춰 나란히 보이게 한다.
  for (const zone of out.zones) zone.h = bottom - y0;
  const w = x - NODE_GAP + CLUSTER_PAD;
  const h = bottom + CLUSTER_PAD - top;
  out.envHeads.push({ envId: env.id, x: 0, y: 0, w });
  out.clusters.push({
    envId: env.id,
    key: `${env.id}/cluster`,
    label,
    x: 0,
    y: top,
    w,
    h,
    empty: false,
  });
  return { ...out, w, h: top + h };
}

function moveBlock(block: EnvBlock, dx: number, dy: number) {
  const shift = <T extends { x: number; y: number }>(r: T) => ({
    ...r,
    x: r.x + dx,
    y: r.y + dy,
  });
  return {
    envHeads: block.envHeads.map(shift),
    clusters: block.clusters.map(shift),
    zones: block.zones.map(shift),
    slabs: block.slabs.map((slab) => ({
      ...shift(slab),
      cells: slab.cells.map(shift),
    })),
  };
}

/** 환경 묶음이 화면에서 차지하는 상자(iso 면 마름모의 외접 상자, 위로 이름 높이만큼 여유). */
function screenBox(block: EnvBlock, mode: "iso" | "flat") {
  if (mode === "flat") return { left: 0, top: 0, w: block.w, h: block.h };
  return {
    left: -block.h * COS,
    top: -LABEL_LIFT,
    w: (block.w + block.h) * COS,
    h: (block.w + block.h) * SIN + LABEL_LIFT,
  };
}

const LABEL_LIFT = 30;
const SCREEN_GAP = 48;

/**
 * 환경을 화면 격자로 놓는다. 열 수는 캔버스(view)에 가장 크게 들어가는 쪽을 고른다.
 * iso 면 화면 위치를 바닥 좌표로 되돌려 놓는다(대각선 계단 대신 화면 가로/세로 줄).
 */
export function layoutNodes(
  environments: FleetEnvironment[],
  mode: "iso" | "flat" = "flat",
  view: { width: number; height: number } = { width: 1200, height: 700 },
): NodeLayout {
  const blocks = environments.map(layoutEnv);
  const boxes = blocks.map((b) => screenBox(b, mode));
  const pack = (cols: number) => {
    const spots: { sx: number; sy: number }[] = [];
    let y = 0;
    let width = 0;
    for (let start = 0; start < boxes.length; start += cols) {
      const row = boxes.slice(start, start + cols);
      let x = 0;
      for (const box of row) {
        spots.push({ sx: x, sy: y });
        x += box.w + SCREEN_GAP;
      }
      width = Math.max(width, x - SCREEN_GAP);
      y += Math.max(...row.map((b) => b.h)) + SCREEN_GAP;
    }
    return { spots, width, height: y - SCREEN_GAP };
  };
  let best = pack(Math.max(1, boxes.length));
  let bestScale = 0;
  for (let cols = 1; cols <= boxes.length; cols++) {
    const packed = pack(cols);
    const scale = Math.min(
      (view.width - 48) / Math.max(packed.width, 1),
      (view.height - 72) / Math.max(packed.height, 1),
    );
    if (scale > bestScale + 0.001) {
      best = packed;
      bestScale = scale;
    }
  }
  const out: NodeLayout = {
    width: best.width,
    height: best.height,
    envHeads: [],
    clusters: [],
    zones: [],
    slabs: [],
  };
  blocks.forEach((block, i) => {
    const box = boxes[i];
    const { sx, sy } = best.spots[i];
    // 화면에서 상자의 왼쪽 위가 (sx, sy) 가 되는 바닥 원점
    const X = sx - box.left;
    const Y = sy - box.top;
    const [dx, dy] =
      mode === "iso"
        ? [(X / COS + Y / SIN) / 2, (Y / SIN - X / COS) / 2]
        : [X, Y];
    const moved = moveBlock(block, dx, dy);
    out.envHeads.push(...moved.envHeads);
    out.clusters.push(...moved.clusters);
    out.zones.push(...moved.zones);
    out.slabs.push(...moved.slabs);
  });
  return out;
}
