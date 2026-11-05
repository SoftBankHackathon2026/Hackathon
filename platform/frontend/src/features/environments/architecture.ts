import type { ComponentLayer, EnvComponent } from "../../api/migrations.ts";

export const NODE_W = 152;
export const NODE_H = 76;
export const GAP_Y = 16;
export const BAND_GAP = 20;
export const CAPTION_H = 24;
export const MIN_GAP_X = 28;
export const LINK_STUB = 44;

const MAIN: ComponentLayer[] = ["entry", "app", "data", "link"];

export type ArchNode = {
  id: string;
  layer: ComponentLayer;
  x: number;
  y: number;
};
export type ArchCaption = { text: string; x: number; y: number };
export type ArchEdge = { from: string; to: string; path: string };
export type ArchLayout = {
  width: number;
  height: number;
  nodes: ArchNode[];
  captions: ArchCaption[];
  edges: ArchEdge[];
  stubs: { id: string; path: string }[];
};

const CAPTIONS: Record<ComponentLayer, string> = {
  entry: "진입점",
  platform: "배포·운영",
  app: "앱",
  migration: "이전 도구",
  data: "데이터",
  link: "환경 간 연결",
};

function bandRows(
  items: EnvComponent[],
  width: number,
  top: number,
  layer: ComponentLayer,
) {
  const perRow = Math.max(1, Math.floor((width + 16) / (NODE_W + 16)));
  const nodes: ArchNode[] = items.map((c, i) => ({
    id: c.id,
    layer,
    x: (i % perRow) * (NODE_W + 16),
    y: top + CAPTION_H + Math.floor(i / perRow) * (NODE_H + GAP_Y),
  }));
  const rows = Math.ceil(items.length / perRow);
  const height = CAPTION_H + rows * NODE_H + Math.max(0, rows - 1) * GAP_Y;
  return { nodes, height };
}

function curve(x1: number, y1: number, x2: number, y2: number) {
  const mid = (x1 + x2) / 2;
  return `M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`;
}

export function layoutArchitecture(
  components: EnvComponent[],
  availableWidth: number,
): ArchLayout {
  const byLayer = (layer: ComponentLayer) =>
    components.filter((c) => c.layer === layer);
  const columns = MAIN.filter((layer) => byLayer(layer).length > 0);
  const minWidth =
    Math.max(columns.length, 1) * NODE_W +
    Math.max(columns.length - 1, 0) * MIN_GAP_X +
    (byLayer("link").length > 0 ? LINK_STUB : 0);
  const width = Math.max(availableWidth, minWidth);
  const nodes: ArchNode[] = [];
  const captions: ArchCaption[] = [];
  let cursor = 0;

  const platform = byLayer("platform");
  if (platform.length > 0) {
    const band = bandRows(platform, width, cursor, "platform");
    captions.push({ text: CAPTIONS.platform, x: 0, y: cursor });
    nodes.push(...band.nodes);
    cursor += band.height + BAND_GAP;
  }

  const hasLink = byLayer("link").length > 0;
  const usable = width - (hasLink ? LINK_STUB : 0);
  const step =
    columns.length > 1 ? (usable - NODE_W) / (columns.length - 1) : 0;
  const mainTop = cursor;
  let mainHeight = 0;
  columns.forEach((layer, ci) => {
    const items = byLayer(layer);
    const x = ci * step;
    captions.push({ text: CAPTIONS[layer], x, y: mainTop });
    items.forEach((c, i) =>
      nodes.push({
        id: c.id,
        layer,
        x,
        y: mainTop + CAPTION_H + i * (NODE_H + GAP_Y),
      }),
    );
    mainHeight = Math.max(
      mainHeight,
      CAPTION_H + items.length * NODE_H + Math.max(0, items.length - 1) * GAP_Y,
    );
  });
  cursor = mainTop + mainHeight;

  const migration = byLayer("migration");
  if (migration.length > 0) {
    cursor += BAND_GAP;
    const band = bandRows(migration, width, cursor, "migration");
    captions.push({ text: CAPTIONS.migration, x: 0, y: cursor });
    nodes.push(...band.nodes);
    cursor += band.height;
  }

  const at = new Map(nodes.map((n) => [n.id, n]));
  const columnOf = (n: ArchNode) => MAIN.indexOf(n.layer);
  const edges: ArchEdge[] = [];
  for (const c of components) {
    for (const depId of c.depends_on) {
      const a = at.get(c.id);
      const b = at.get(depId);
      if (!a || !b) continue;
      const aMain = columnOf(a) >= 0;
      const bMain = columnOf(b) >= 0;
      let path: string | null = null;
      let from = c.id;
      let to = depId;
      if (aMain && bMain) {
        if (a.x === b.x) {
          const [upper, lower] = a.y < b.y ? [a, b] : [b, a];
          [from, to] = upper === b ? [depId, c.id] : [c.id, depId];
          const cx = upper.x + NODE_W / 2;
          path = `M${cx} ${upper.y + NODE_H} L${cx} ${lower.y}`;
        } else {
          const [left, right] = a.x < b.x ? [a, b] : [b, a];
          [from, to] = left === a ? [c.id, depId] : [depId, c.id];
          path = curve(
            left.x + NODE_W,
            left.y + NODE_H / 2,
            right.x,
            right.y + NODE_H / 2,
          );
        }
      } else if (aMain !== bMain) {
        const band = aMain ? b : a;
        const main = aMain ? a : b;
        const bandIsBelow = band.layer === "migration";
        const bx = band.x + NODE_W / 2;
        const mx = main.x + NODE_W / 2;
        const by = bandIsBelow ? band.y : band.y + NODE_H;
        const my = bandIsBelow ? main.y + NODE_H : main.y;
        const mid = (by + my) / 2;
        path = `M${bx} ${by} C${bx} ${mid} ${mx} ${mid} ${mx} ${my}`;
        [from, to] = bandIsBelow ? [band.id, main.id] : [main.id, band.id];
      }
      if (path) edges.push({ from, to, path });
    }
  }

  const stubs = nodes
    .filter((n) => n.layer === "link")
    .map((n) => ({
      id: n.id,
      path: `M${n.x + NODE_W} ${n.y + NODE_H / 2} L${n.x + NODE_W + LINK_STUB - 6} ${n.y + NODE_H / 2}`,
    }));

  return { width, height: cursor, nodes, captions, edges, stubs };
}
