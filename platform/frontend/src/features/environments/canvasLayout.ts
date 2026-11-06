import type { EnvComponent, FleetEnvironment } from "../../api/migrations.ts";
import {
  buildGroups,
  crossLinks,
  type ContainerKind,
  type GroupNode,
  type PathNode,
} from "./canvasModel.ts";

export const CELL = 96;
export const CELL_GAP = 18;
export const GROUP_PAD = 14;
export const GROUP_HEAD = 46;
export const GROUP_GAP = 16;
export const ENV_HEAD = 48;
export const ENV_GAP = 90;
export const CANVAS_MARGIN = 24;
const ENV_MAX_INNER = 520;
const MAX_COLS = 4;
const LABEL_CHAR = 8;

export type GroupRect = {
  key: string;
  envId: string;
  label: string;
  kind: ContainerKind;
  depth: number;
  x: number;
  y: number;
  w: number;
  h: number;
};
export type TileRect = {
  id: string;
  envId: string;
  x: number;
  y: number;
  w: number;
  h: number;
};
export type Point = [number, number];
export type Route = {
  envId: string;
  from: string;
  to: string;
  points: [Point, Point];
};
export type CanvasLayout = {
  width: number;
  height: number;
  groups: GroupRect[];
  tiles: TileRect[];
  envHeads: { envId: string; x: number; y: number; w: number }[];
  routes: Route[];
  links: { route: Route; label: string; labelAt: Point }[];
};

type Gutter = { side: "left" | "right"; count: number };
type Placement =
  | { kind: "tiles"; dx: number; dy: number }
  | { kind: "group"; dx: number; dy: number; layout: GroupLayout };
type GroupLayout = {
  node: GroupNode;
  w: number;
  h: number;
  placements: Placement[];
  cols: number;
  tiles: EnvComponent[];
  gutter: Gutter | null;
};
type LinkSlot = {
  envId: string;
  x: number;
  top: number;
  components: EnvComponent[];
};

const regular = (node: GroupNode) =>
  node.tiles.filter((t) => t.layer !== "link");

function findHost(root: GroupNode, side: "left" | "right"): GroupNode {
  if (side === "right") return root;
  const queue = [root];
  while (queue.length > 0) {
    const node = queue.shift() as GroupNode;
    if (node.kind === "network" && node.label.startsWith("VPC")) return node;
    queue.push(...node.groups);
  }
  return root;
}

function sizeGroup(
  node: GroupNode,
  maxWidth: number,
  host: { key: string; gutter: Gutter } | null,
): GroupLayout {
  const inner = Math.max(maxWidth - 2 * GROUP_PAD, CELL);
  const tiles = regular(node);
  const items: {
    kind: "tiles" | "group";
    w: number;
    h: number;
    layout?: GroupLayout;
  }[] = [];
  const fit = Math.max(1, Math.floor((inner + CELL_GAP) / (CELL + CELL_GAP)));
  const cols = Math.max(1, Math.min(tiles.length, MAX_COLS, fit));
  if (tiles.length > 0) {
    const rows = Math.ceil(tiles.length / cols);
    items.push({
      kind: "tiles",
      w: cols * CELL + (cols - 1) * CELL_GAP,
      h: rows * CELL + (rows - 1) * CELL_GAP,
    });
  }
  for (const child of node.groups) {
    const layout = sizeGroup(child, inner, host);
    items.push({ kind: "group", w: layout.w, h: layout.h, layout });
  }
  const placements: Placement[] = [];
  let x = 0;
  let y = 0;
  let rowHeight = 0;
  let usedWidth = 0;
  for (const item of items) {
    if (x > 0 && x + item.w > inner) {
      y += rowHeight + GROUP_GAP;
      x = 0;
      rowHeight = 0;
    }
    placements.push(
      item.kind === "tiles"
        ? { kind: "tiles", dx: x, dy: y }
        : { kind: "group", dx: x, dy: y, layout: item.layout as GroupLayout },
    );
    x += item.w + GROUP_GAP;
    rowHeight = Math.max(rowHeight, item.h);
    usedWidth = Math.max(usedWidth, x - GROUP_GAP);
  }
  const labelWidth = node.label.length * LABEL_CHAR + 2 * GROUP_PAD + 22;
  let w = Math.max(usedWidth + 2 * GROUP_PAD, labelWidth);
  let h = GROUP_HEAD + y + rowHeight + GROUP_PAD;
  const gutter = host && host.key === node.key ? host.gutter : null;
  if (gutter) {
    const extra = CELL + GROUP_GAP;
    w += extra;
    if (gutter.side === "left")
      for (const placement of placements) placement.dx += extra;
    h = Math.max(
      h,
      GROUP_HEAD +
        gutter.count * CELL +
        (gutter.count - 1) * CELL_GAP +
        2 * GROUP_PAD +
        CELL,
    );
  }
  return { node, w, h, placements, cols, tiles, gutter };
}

function place(
  layout: GroupLayout,
  envId: string,
  x: number,
  y: number,
  groups: GroupRect[],
  tiles: TileRect[],
  slots: LinkSlot[],
  links: EnvComponent[],
) {
  const { node } = layout;
  groups.push({
    key: node.key,
    envId,
    label: node.label,
    kind: node.kind,
    depth: node.depth,
    x,
    y,
    w: layout.w,
    h: layout.h,
  });
  const originX = x + GROUP_PAD;
  const originY = y + GROUP_HEAD;
  if (layout.gutter)
    slots.push({
      envId,
      x:
        layout.gutter.side === "left"
          ? originX
          : x + layout.w - GROUP_PAD - CELL,
      top: originY,
      components: links,
    });
  for (const placement of layout.placements) {
    if (placement.kind === "tiles") {
      layout.tiles.forEach((tile, i) => {
        tiles.push({
          id: tile.id,
          envId,
          x: originX + placement.dx + (i % layout.cols) * (CELL + CELL_GAP),
          y:
            originY +
            placement.dy +
            Math.floor(i / layout.cols) * (CELL + CELL_GAP),
          w: CELL,
          h: CELL,
        });
      });
    } else {
      place(
        placement.layout,
        envId,
        originX + placement.dx,
        originY + placement.dy,
        groups,
        tiles,
        slots,
        links,
      );
    }
  }
}

export function tileCenter(tile: TileRect): Point {
  return [tile.x + tile.w / 2, tile.y + tile.h / 2];
}

export function pointAlong(a: Point, b: Point, ratio: number): Point {
  return [a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio];
}

export function layoutCanvas(environments: FleetEnvironment[]): CanvasLayout {
  const groups: GroupRect[] = [];
  const tiles: TileRect[] = [];
  const slots: LinkSlot[] = [];
  const envHeads: CanvasLayout["envHeads"] = [];
  const trees = environments.map((env) => buildGroups(env));
  let cursor = CANVAS_MARGIN;
  let tallest = 0;
  environments.forEach((env, i) => {
    const links = env.components.filter((c) => c.layer === "link");
    const side = i === 0 ? "right" : "left";
    const hostNode = links.length > 0 ? findHost(trees[i], side) : null;
    const layout = sizeGroup(
      trees[i],
      ENV_MAX_INNER + 2 * GROUP_PAD,
      hostNode
        ? { key: hostNode.key, gutter: { side, count: links.length } }
        : null,
    );
    if (i > 0) cursor += ENV_GAP;
    envHeads.push({ envId: env.id, x: cursor, y: CANVAS_MARGIN, w: layout.w });
    place(
      layout,
      env.id,
      cursor,
      CANVAS_MARGIN + ENV_HEAD,
      groups,
      tiles,
      slots,
      links,
    );
    cursor += layout.w;
    tallest = Math.max(tallest, layout.h);
  });
  if (slots.length > 0) {
    const linkTop = Math.max(...slots.map((s) => s.top)) + GROUP_PAD;
    for (const slot of slots)
      slot.components.forEach((component, i) =>
        tiles.push({
          id: component.id,
          envId: slot.envId,
          x: slot.x,
          y: linkTop + i * (CELL + CELL_GAP),
          w: CELL,
          h: CELL,
        }),
      );
    for (const group of groups) {
      const need = linkTop + CELL + GROUP_PAD - group.y;
      if (group.depth === 0 && need > group.h) group.h = need;
    }
    tallest = Math.max(
      tallest,
      ...groups.filter((g) => g.depth === 0).map((g) => g.h),
    );
  }
  const byId = new Map(tiles.map((t) => [`${t.envId}:${t.id}`, t]));
  const routes: Route[] = [];
  for (const env of environments)
    for (const component of env.components)
      for (const dep of component.depends_on) {
        const from = byId.get(`${env.id}:${component.id}`);
        const to = byId.get(`${env.id}:${dep}`);
        if (from && to)
          routes.push({
            envId: env.id,
            from: from.id,
            to: to.id,
            points: [tileCenter(from), tileCenter(to)],
          });
      }
  const links = crossLinks(environments).flatMap((link) => {
    const from = byId.get(`${link.from.envId}:${link.from.id}`);
    const to = byId.get(`${link.to.envId}:${link.to.id}`);
    if (!from || !to) return [];
    const points: [Point, Point] = [tileCenter(from), tileCenter(to)];
    return [
      {
        route: { envId: from.envId, from: from.id, to: to.id, points },
        label: link.label,
        labelAt: pointAlong(points[0], points[1], 0.5),
      },
    ];
  });
  return {
    width: cursor + CANVAS_MARGIN,
    height: CANVAS_MARGIN + ENV_HEAD + tallest + CANVAS_MARGIN,
    groups,
    tiles,
    envHeads,
    routes,
    links,
  };
}

export function routeNodes(
  nodes: PathNode[],
  layout: CanvasLayout,
): { from: PathNode; to: PathNode; points: [Point, Point] }[] {
  const byId = new Map(layout.tiles.map((t) => [`${t.envId}:${t.id}`, t]));
  const result: { from: PathNode; to: PathNode; points: [Point, Point] }[] = [];
  for (let i = 1; i < nodes.length; i += 1) {
    const from = byId.get(`${nodes[i - 1].envId}:${nodes[i - 1].id}`);
    const to = byId.get(`${nodes[i].envId}:${nodes[i].id}`);
    if (from && to)
      result.push({
        from: nodes[i - 1],
        to: nodes[i],
        points: [tileCenter(from), tileCenter(to)],
      });
  }
  return result;
}
