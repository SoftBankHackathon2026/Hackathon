import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import type { FleetEnvironment } from "../../api/migrations";
import { useElementWidth } from "../../components/charts/useElementWidth";
import { containerPaint, LEVEL_COLORS } from "./blockStyle";
import type { Point } from "./canvasLayout";
import {
  clusterState,
  nodeState,
  percent,
  resourceRows,
  shortNodeName,
  type PodCell,
} from "./clusterModel";
import {
  blockFaces,
  fitView,
  floorTransform,
  polygonPoints,
  project,
  shade,
  zoomAt,
  type ViewMode,
} from "./iso";
import {
  NODE_PAD,
  layoutNodes,
  type NodeLayout,
  type NodeSlab,
} from "./nodeLayout";
import type { TileRef } from "./IsoCanvas";
import { textWidth } from "./securityModel";
import { podAge, podLabel } from "./podModel";
import { usePanZoom } from "./usePanZoom";

export type NodeRef = { envId: string; node: string | null };

const SLAB_H = 8;
const POD_EDGE = 11;
const POD_H = 6;
const ZONE_COLOR = LEVEL_COLORS.region;

function bounds(layout: NodeLayout, mode: ViewMode) {
  const corners: Point[] = [
    ...layout.clusters,
    ...layout.envHeads.map((h) => ({ ...h, h: 40 })),
  ].flatMap((r) => [
    project(mode, r.x, r.y),
    project(mode, r.x + r.w, r.y),
    project(mode, r.x, r.y + r.h),
    project(mode, r.x + r.w, r.y + r.h),
  ]);
  const xs = corners.map((c) => c[0]);
  const ys = corners.map((c) => c[1]);
  return {
    minX: Math.min(...xs),
    maxX: Math.max(...xs),
    minY: Math.min(...ys) - 30,
    // 아래 범례 자리만큼 여유
    maxY: Math.max(...ys) + 44,
  };
}

/** 바닥 위 직사각형 상자의 면(노드 판). */
function boxFaces(
  mode: ViewMode,
  r: { x: number; y: number; w: number; h: number },
  height: number,
) {
  const p = (x: number, y: number, lift = 0): Point => {
    const [px, py] = project(mode, x, y);
    return [px, py - lift];
  };
  const n = [r.x, r.y] as const;
  const e = [r.x + r.w, r.y] as const;
  const s = [r.x + r.w, r.y + r.h] as const;
  const w = [r.x, r.y + r.h] as const;
  return {
    top: [p(...n, height), p(...e, height), p(...s, height), p(...w, height)],
    left: [p(...w), p(...s), p(...s, height), p(...w, height)],
    right: [p(...s), p(...e), p(...e, height), p(...s, height)],
    silhouette: [
      p(...n, height),
      p(...e, height),
      p(...e),
      p(...s),
      p(...w),
      p(...w, height),
    ],
  };
}

/** 바닥 면 위(들어 올린 높이 lift)에 그리는 묶음: 글자와 막대가 판 윗면에 눕는다. */
function onSurface(mode: ViewMode, lift: number) {
  const plane = floorTransform(mode);
  return `translate(0 ${-lift})${plane ? ` ${plane}` : ""}`;
}

type Hover = {
  x: number;
  y: number;
  cell: PodCell;
  node: string | null;
  envId: string;
};

export function NodeCanvas({
  environments,
  mode,
  onModeChange,
  query,
  selectedComponent,
  selectedNode,
  onSelectComponent,
  onSelectNode,
  onClear,
  height: maxHeight,
}: {
  environments: FleetEnvironment[];
  mode: ViewMode;
  onModeChange: (mode: ViewMode) => void;
  query: string;
  selectedComponent: TileRef | null;
  selectedNode: NodeRef | null;
  onSelectComponent: (ref: TileRef) => void;
  onSelectNode: (ref: NodeRef) => void;
  onClear: () => void;
  height: number;
}) {
  const { ref, width } = useElementWidth(900);
  const svgRef = useRef<SVGSVGElement>(null);
  const { view, setView, moved, handlers } = usePanZoom(svgRef, onClear);
  const [hover, setHover] = useState<Hover | null>(null);
  const [hoverComponent, setHoverComponent] = useState<string | null>(null);
  const layout = layoutNodes(environments, mode, {
    width,
    height: maxHeight,
  });
  const box = bounds(layout, mode);
  const fitScale = Math.min(
    (width - 48) / Math.max(box.maxX - box.minX, 1),
    1.4,
  );
  const height = Math.round(
    Math.min(maxHeight, Math.max(480, (box.maxY - box.minY) * fitScale + 72)),
  );
  const fit = () => fitView(box, width, height);

  useEffect(() => {
    setView(fitView(box, width, height));
    // 환경 수, 보기 방식, 크기가 바뀔 때만 다시 맞춘다(30초 갱신마다 시점을 되돌리지 않는다).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, layout.width, layout.height, width, height]);

  const envOf = (id: string) => environments.find((e) => e.id === id);
  const activeComponent =
    hoverComponent ??
    (selectedComponent
      ? `${selectedComponent.envId}:${selectedComponent.id}`
      : null);
  const q = query.trim().toLowerCase();
  const matches = (slab: NodeSlab, cell: PodCell) => {
    if (!q) return true;
    const component = envOf(slab.envId)?.components.find(
      (c) => c.id === cell.componentId,
    );
    return [cell.pod?.name, component?.name, slab.node?.name].some((t) =>
      t?.toLowerCase().includes(q),
    );
  };
  const slabMatches = (slab: NodeSlab) =>
    !q ||
    slab.node?.name.toLowerCase().includes(q) ||
    slab.cells.some((cell) => cell.componentId && matches(slab, cell));

  const envFont = Math.min(34, Math.max(15, 16 / view.k));
  const plateFont = Math.min(24, Math.max(12, 12.5 / view.k));
  const nameFont = Math.min(13, Math.max(10, 11 / view.k));
  // 노드 판 윗면의 자원 막대 글자: 너무 작아 읽히지 않으면 막대만 그린다.
  const showBarText = view.k >= 0.6;
  const fitText = (text: string, max: number, size: number) => {
    if (textWidth(text, size) <= max) return text;
    let out = text;
    while (out.length > 1 && textWidth(`${out}…`, size) > max)
      out = out.slice(0, -1);
    return `${out}…`;
  };
  const slabs = [...layout.slabs].sort((a, b) => a.x + a.y - (b.x + b.y));

  const pointAt = (event: ReactPointerEvent): Point => {
    const rect = ref.current?.getBoundingClientRect();
    return [
      event.clientX - (rect?.left ?? 0),
      event.clientY - (rect?.top ?? 0),
    ];
  };

  return (
    <div className="iso-wrap node-wrap" ref={ref} style={{ height }}>
      <svg
        ref={svgRef}
        className="iso-svg"
        width={width}
        height={height}
        {...handlers}
      >
        <defs>
          <pattern
            id="node-grid"
            width={48}
            height={48}
            patternUnits="userSpaceOnUse"
          >
            <path d="M48 0H0V48" className="iso-grid-line" />
          </pattern>
        </defs>
        <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
          <g transform={floorTransform(mode)}>
            <rect
              x={-3000}
              y={-3000}
              width={9000}
              height={9000}
              fill="url(#node-grid)"
            />
            {layout.envHeads.map((head) => {
              const env = envOf(head.envId);
              return (
                <text
                  key={head.envId}
                  x={head.x}
                  y={head.y + 6 + envFont * 0.82}
                  className="floor-env"
                  style={{ fontSize: envFont }}
                >
                  <title>{env?.name}</title>
                  {fitText(env?.name ?? "", head.w - envFont * 4, envFont)}
                  <tspan className="floor-env-sub">
                    {"  "}
                    {env?.role === "legacy" ? "레거시" : "신규"}
                  </tspan>
                </text>
              );
            })}
            {layout.clusters.map((plate) => {
              const paint = containerPaint({
                kind: "cluster",
                label: plate.label,
              });
              const env = envOf(plate.envId);
              const state = clusterState(env?.cluster);
              const picked =
                selectedNode?.envId === plate.envId &&
                selectedNode.node === null;
              const version = env?.cluster?.version;
              const text = `${plate.label}${version ? `  ${version}` : ""}`;
              return (
                <g
                  key={plate.key}
                  className="node-plate"
                  role="button"
                  tabIndex={0}
                  aria-label={`${env?.name ?? ""} 클러스터`}
                  aria-pressed={picked}
                  onClick={(event) => {
                    event.stopPropagation();
                    if (!moved())
                      onSelectNode({ envId: plate.envId, node: null });
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onSelectNode({ envId: plate.envId, node: null });
                    }
                  }}
                >
                  <rect
                    x={plate.x}
                    y={plate.y}
                    width={plate.w}
                    height={plate.h}
                    rx="3"
                    fill={paint.fill}
                    stroke={paint.stroke}
                    strokeWidth={picked ? 2.6 : 1.3}
                  />
                  <circle
                    cx={plate.x + 10 + plateFont * 0.35}
                    cy={plate.y + 6 + plateFont * 0.5}
                    r={plateFont * 0.3}
                    className={`label-dot tone-${state.tone}`}
                  />
                  <text
                    x={plate.x + 10 + plateFont}
                    y={plate.y + 6 + plateFont * 0.82}
                    className="floor-label"
                    fill={paint.ink}
                    style={{ fontSize: plateFont }}
                  >
                    <title>{`${text}, ${state.label}`}</title>
                    {fitText(text, plate.w - 20 - plateFont, plateFont)}
                  </text>
                  {plate.empty && (
                    <text
                      x={plate.x + plate.w / 2}
                      y={plate.y + plate.h / 2 + 10}
                      textAnchor="middle"
                      className="node-empty"
                      style={{ fontSize: 13 }}
                    >
                      {env?.connected === false
                        ? "연결 끊김"
                        : "노드 정보를 아직 받지 못했어요"}
                    </text>
                  )}
                </g>
              );
            })}
            {layout.zones.map((zone) => (
              <g key={zone.key}>
                <rect
                  x={zone.x}
                  y={zone.y}
                  width={zone.w}
                  height={zone.h}
                  rx="3"
                  fill="rgba(138,99,210,0.04)"
                  stroke={ZONE_COLOR}
                  strokeWidth="1.2"
                  strokeDasharray="8 5"
                />
                <text
                  x={zone.x + 8}
                  y={zone.y + 6 + Math.min(plateFont, 13) * 0.82}
                  className="floor-label"
                  fill={shade(ZONE_COLOR, 0.8)}
                  style={{ fontSize: Math.min(plateFont, 13) }}
                >
                  <title>{zone.label}</title>
                  {fitText(zone.label, zone.w - 16, Math.min(plateFont, 13))}
                </text>
              </g>
            ))}
          </g>
          {slabs.map((slab) => {
            const lift = mode === "iso" ? SLAB_H : 0;
            const faces = boxFaces(mode, slab, lift);
            const node = slab.node;
            const state = node
              ? nodeState(node)
              : { tone: "warn" as const, label: "배치 안 됨" };
            const picked =
              node !== null &&
              selectedNode?.envId === slab.envId &&
              selectedNode.node === node.name;
            const dim = !slabMatches(slab);
            const rows = node ? resourceRows(node) : [];
            const name = node
              ? shortNodeName(node.name)
              : "아직 노드에 배치되지 않음";
            const cubes = [...slab.cells].sort(
              (a, b) => a.x + a.y - (b.x + b.y),
            );
            const cube = blockFaces(mode, POD_EDGE, mode === "iso" ? POD_H : 0);
            const barX = slab.x + NODE_PAD + 34;
            const barW = slab.w - NODE_PAD * 2 - 34 - 30;
            return (
              <g
                key={`${slab.envId}:${slab.key}`}
                className={`node-slab tone-${state.tone}${node ? "" : " unplaced"}${dim ? " dim" : ""}`}
              >
                <g
                  className="ns-body"
                  role={node ? "button" : undefined}
                  tabIndex={node ? 0 : undefined}
                  aria-label={node ? `노드 ${node.name}` : name}
                  aria-pressed={node ? picked : undefined}
                  onClick={(event) => {
                    event.stopPropagation();
                    if (node && !moved())
                      onSelectNode({ envId: slab.envId, node: node.name });
                  }}
                  onKeyDown={(event) => {
                    if (node && (event.key === "Enter" || event.key === " ")) {
                      event.preventDefault();
                      onSelectNode({ envId: slab.envId, node: node.name });
                    }
                  }}
                >
                  {mode === "iso" && node && (
                    <>
                      <polygon
                        className="ns-l"
                        points={polygonPoints(faces.left)}
                      />
                      <polygon
                        className="ns-r"
                        points={polygonPoints(faces.right)}
                      />
                    </>
                  )}
                  <polygon className="ns-t" points={polygonPoints(faces.top)} />
                  <g transform={onSurface(mode, lift)}>
                    <circle
                      cx={slab.x + NODE_PAD + nameFont * 0.3}
                      cy={slab.y + NODE_PAD + nameFont * 0.42}
                      r={nameFont * 0.28}
                      className={`label-dot tone-${state.tone}`}
                    />
                    <text
                      x={slab.x + NODE_PAD + nameFont * 0.85}
                      y={slab.y + NODE_PAD + nameFont * 0.8}
                      className="ns-name"
                      style={{ fontSize: nameFont }}
                    >
                      <title>
                        {node
                          ? `${node.name}, ${state.label}${node.pods !== null ? `, 파드 ${node.pods}개` : ""}`
                          : name}
                      </title>
                      {fitText(
                        name,
                        slab.w - NODE_PAD * 2 - nameFont,
                        nameFont,
                      )}
                    </text>
                    {rows.map((row, i) => {
                      const y = slab.y + NODE_PAD + 22 + i * 13;
                      const fill = row.usageShare ?? row.requestShare;
                      return (
                        <g
                          key={row.key}
                          className={`ns-bar tone-${row.tone}${row.usageShare === null ? " requested" : ""}`}
                        >
                          {showBarText && (
                            <text
                              x={slab.x + NODE_PAD}
                              y={y + 4}
                              className="ns-bar-label"
                              style={{ fontSize: 9 }}
                            >
                              {row.key === "cpu" ? "CPU" : "메모리"}
                            </text>
                          )}
                          <rect
                            className="ns-track"
                            x={barX}
                            y={y - 2}
                            width={barW}
                            height={6}
                            rx="1.5"
                          />
                          {fill !== null && (
                            <rect
                              className="ns-fill"
                              x={barX}
                              y={y - 2}
                              width={Math.max(1, Math.min(1, fill) * barW)}
                              height={6}
                              rx="1.5"
                            />
                          )}
                          {row.usageShare !== null &&
                            row.requestShare !== null && (
                              <line
                                className="ns-request"
                                x1={barX + Math.min(1, row.requestShare) * barW}
                                x2={barX + Math.min(1, row.requestShare) * barW}
                                y1={y - 4}
                                y2={y + 6}
                              />
                            )}
                          {showBarText && (
                            <text
                              x={slab.x + slab.w - NODE_PAD}
                              y={y + 4}
                              textAnchor="end"
                              className="ns-bar-value"
                              style={{ fontSize: 9 }}
                            >
                              {percent(fill)}
                            </text>
                          )}
                        </g>
                      );
                    })}
                    {!node && showBarText && (
                      <text
                        x={slab.x + NODE_PAD}
                        y={slab.y + NODE_PAD + 30}
                        className="ns-bar-label"
                        style={{ fontSize: 9 }}
                      >
                        스케줄러가 노드를 찾는 중
                      </text>
                    )}
                  </g>
                  {picked && (
                    <polygon
                      points={polygonPoints(faces.silhouette)}
                      className="block-ring selected"
                    />
                  )}
                </g>
                {cubes.map((cell) => {
                  const [px, py] = project(mode, cell.x, cell.y);
                  const key = cell.componentId
                    ? `${slab.envId}:${cell.componentId}`
                    : null;
                  const lit = key !== null && key === activeComponent;
                  const faded =
                    (activeComponent !== null && !lit) ||
                    (q !== "" && !matches(slab, cell));
                  return (
                    <g
                      key={cell.key}
                      className={`pod-cube tone-${cell.tone}${lit ? " lit" : ""}${faded ? " faded" : ""}`}
                      data-clickable={cell.componentId ? "" : undefined}
                      transform={`translate(${px} ${py - lift})`}
                      onPointerEnter={(event) => {
                        const [x, y] = pointAt(event);
                        setHover({
                          x,
                          y,
                          cell,
                          node: node?.name ?? null,
                          envId: slab.envId,
                        });
                        setHoverComponent(key);
                      }}
                      onPointerLeave={() => {
                        setHover(null);
                        setHoverComponent(null);
                      }}
                      onClick={(event) => {
                        event.stopPropagation();
                        if (cell.componentId && !moved())
                          onSelectComponent({
                            envId: slab.envId,
                            id: cell.componentId,
                          });
                      }}
                    >
                      {mode === "iso" ? (
                        <>
                          <polygon
                            className="pc-l"
                            points={polygonPoints(cube.left)}
                          />
                          <polygon
                            className="pc-r"
                            points={polygonPoints(cube.right)}
                          />
                          <polygon
                            className="pc-t"
                            points={polygonPoints(cube.top)}
                          />
                        </>
                      ) : (
                        <rect
                          className="pc-t"
                          x={-POD_EDGE / 2}
                          y={-POD_EDGE / 2}
                          width={POD_EDGE}
                          height={POD_EDGE}
                          rx="2"
                        />
                      )}
                      {lit && (
                        <polygon
                          className="pc-ring"
                          points={polygonPoints(cube.silhouette)}
                        />
                      )}
                    </g>
                  );
                })}
                {slab.hidden > 0 &&
                  (() => {
                    const last = slab.cells[slab.cells.length - 1];
                    const [px, py] = project(mode, last.x + 14, last.y);
                    return (
                      <text
                        x={px}
                        y={py - lift + 3}
                        className="pod-more"
                        style={{ fontSize: Math.max(9, 10 / view.k) }}
                      >
                        +{slab.hidden}
                      </text>
                    );
                  })()}
              </g>
            );
          })}
        </g>
      </svg>
      {hover && <PodTip hover={hover} env={envOf(hover.envId)} width={width} />}
      <div className="canvas-legend node-legend" role="group" aria-label="범례">
        <span className="legend-row">
          <i className="legend-cube tone-ok" />
          정상
        </span>
        <span className="legend-row">
          <i className="legend-cube tone-warn" />
          시작 중/준비 안 됨
        </span>
        <span className="legend-row">
          <i className="legend-cube tone-bad" />
          문제
        </span>
        <span className="legend-row">
          <i className="legend-cube tone-other" />
          구성 요소 밖(시스템 등)
        </span>
        <span className="legend-row">
          <i className="legend-bar" />
          CPU/메모리 사용률(실측 없으면 요청량)
        </span>
      </div>
      <div className="zoom-controls" role="group" aria-label="보기 조절">
        <button
          type="button"
          aria-label="확대"
          onClick={() => setView((v) => zoomAt(v, 1.25, width / 2, height / 2))}
        >
          +
        </button>
        <button
          type="button"
          aria-label="축소"
          onClick={() => setView((v) => zoomAt(v, 0.8, width / 2, height / 2))}
        >
          −
        </button>
        <button
          type="button"
          aria-label="화면에 맞춤"
          onClick={() => setView(fit())}
        >
          <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
            <path
              d="M1 5V1h4M9 1h4v4M13 9v4H9M5 13H1V9"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
            />
          </svg>
        </button>
        <button
          type="button"
          aria-label={mode === "iso" ? "2D로 보기" : "3D로 보기"}
          onClick={() => onModeChange(mode === "iso" ? "flat" : "iso")}
        >
          {mode === "iso" ? "2D" : "3D"}
        </button>
      </div>
    </div>
  );
}

/** 파드에 올렸을 때 뜨는 작은 카드: 이름, 구성 요소, 상태, 재시작, 노드. */
function PodTip({
  hover,
  env,
  width,
}: {
  hover: Hover;
  env: FleetEnvironment | undefined;
  width: number;
}) {
  const { cell } = hover;
  const component = env?.components.find((c) => c.id === cell.componentId);
  const pod = cell.pod;
  const left = Math.min(hover.x + 14, width - 280);
  return (
    <div className="pod-tip" style={{ left, top: hover.y + 14 }} role="tooltip">
      {pod && component ? (
        <>
          <b className="pt-name">{pod.name}</b>
          <span className="pt-row">
            <span>{component.name}</span>
            <span className={`pt-state tone-${cell.tone}`}>
              {podLabel(pod)}
            </span>
          </span>
          <span className="pt-sub">
            {[
              `재시작 ${pod.restarts}`,
              podAge(pod.started_at)
                ? `${podAge(pod.started_at)} 전 시작`
                : null,
              hover.node ? `노드 ${shortNodeName(hover.node)}` : null,
            ]
              .filter(Boolean)
              .join(", ")}
          </span>
          <span className="pt-hint">눌러서 구성 요소 상세 보기</span>
        </>
      ) : (
        <span className="pt-sub">
          구성 요소에 속하지 않은 파드(kube-system 등)
        </span>
      )}
    </div>
  );
}
