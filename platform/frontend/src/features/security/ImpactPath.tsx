import { kindColor } from "../environments/blockStyle";
import type { PathColumn } from "./pathModel";

const GLYPHS: Record<string, string> = {
  internet:
    "M-6 0a6 6 0 1 0 12 0a6 6 0 1 0-12 0M-6 0h12M0-6c2.6 3 2.6 9 0 12M0-6c-2.6 3-2.6 9 0 12",
  entry: "M-1-6h6v12h-6M-7 0h7M-3-3l3 3-3 3",
  workload: "M-6-3.5l6-3 6 3v7l-6 3-6-3zM-6-3.5l6 3 6-3M0-0.5v7",
  data: "M-5-4a5 2 0 1 0 10 0a5 2 0 1 0-10 0M-5-4v8a5 2 0 0 0 10 0v-8M-5 0a5 2 0 0 0 10 0",
};

function glyphFor(layer: string) {
  if (layer === "internet") return GLYPHS.internet;
  if (layer === "entry") return GLYPHS.entry;
  if (layer === "data") return GLYPHS.data;
  return GLYPHS.workload;
}

/** 밝은 대표색(앱·플랫폼)은 선이 안 보이니 회색 선으로 바꾼다. */
function strokeFor(hex: string) {
  const n = parseInt(hex.slice(1), 16);
  const lum =
    (0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255)) / 255;
  return lum > 0.75 ? "#6b7684" : hex;
}

function short(name: string, max = 13) {
  return name.length > max ? `${name.slice(0, max - 1)}…` : name;
}

/** 구성 요소 아이콘 타일(목록용): 종류색 옅은 면 + 계층 그림. 영향 경로 노드와 같은 그림. */
export function NodeIcon({ layer, kind }: { layer: string; kind: string }) {
  const color = layer === "internet" ? "#2784d5" : kindColor(kind);
  const stroke = strokeFor(color);
  return (
    <svg className="node-icon" viewBox="-15 -15 30 30" aria-hidden="true">
      <rect
        x={-15}
        y={-15}
        width={30}
        height={30}
        rx={8}
        fill={color}
        fillOpacity={0.14}
      />
      <path
        d={glyphFor(layer)}
        fill="none"
        stroke={stroke}
        strokeWidth={1.4}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

const ROW = 58;
const TOP = 26;
const R = 15;

/** 영향 경로 그림(Wiz 공격 경로 방식). 열: 인터넷 → 진입점 → 워크로드 → 데이터, 문제 리소스엔 빨간 표식. */
export function ImpactPath({
  columns,
  edges,
}: {
  columns: PathColumn[];
  edges: { from: string; to: string }[];
}) {
  const width = 400;
  const rows = Math.max(...columns.map((c) => c.nodes.length));
  const height = TOP + rows * ROW + 4;
  const pos = new Map<string, { x: number; y: number }>();
  const gap = columns.length > 1 ? (width - 72) / (columns.length - 1) : 0;
  columns.forEach((col, i) => {
    const x = columns.length > 1 ? 36 + i * gap : width / 2;
    const offset = TOP + ((rows - col.nodes.length) * ROW) / 2;
    col.nodes.forEach((node, j) =>
      pos.set(node.id, { x, y: offset + j * ROW + R + 2 }),
    );
  });
  return (
    <svg
      className="impact-path"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={columns
        .map((c) => `${c.title}: ${c.nodes.map((n) => n.name).join(", ")}`)
        .join(" → ")}
    >
      {columns.map((col) => (
        <text
          key={col.key}
          className="ip-col"
          x={pos.get(col.nodes[0].id)?.x}
          y={11}
          textAnchor="middle"
        >
          {col.title}
        </text>
      ))}
      {edges.map((edge) => {
        const a = pos.get(edge.from);
        const b = pos.get(edge.to);
        if (!a || !b) return null;
        const mid = (a.x + b.x) / 2;
        return (
          <path
            key={`${edge.from}-${edge.to}`}
            className="ip-edge"
            d={`M${a.x + R} ${a.y}C${mid} ${a.y} ${mid} ${b.y} ${b.x - R} ${b.y}`}
          />
        );
      })}
      {columns.flatMap((col) =>
        col.nodes.map((node) => {
          const p = pos.get(node.id)!;
          const color =
            node.layer === "internet" ? "#2784d5" : kindColor(node.kind);
          const stroke = strokeFor(color);
          return (
            <g key={node.id} transform={`translate(${p.x} ${p.y})`}>
              <title>{node.name}</title>
              <circle r={R} fill="#fff" />
              <circle
                r={R}
                fill={color}
                fillOpacity={0.14}
                stroke={stroke}
                strokeWidth={1.6}
              />
              <path
                d={glyphFor(node.layer)}
                fill="none"
                stroke={stroke}
                strokeWidth={1.4}
                strokeLinecap="round"
                strokeLinejoin="round"
              />
              {node.hit && (
                <circle className="ip-hit" cx={R - 3} cy={-R + 3} r={4.5} />
              )}
              <text className="ip-label" y={R + 13} textAnchor="middle">
                {short(node.name)}
              </text>
            </g>
          );
        }),
      )}
    </svg>
  );
}
