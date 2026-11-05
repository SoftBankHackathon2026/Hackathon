import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import type { EnvComponent, FleetEnvironment } from "../../api/migrations";
import { useElementWidth } from "../../components/charts/useElementWidth";
import {
  routeNodes,
  tileCenter,
  pointAlong,
  type CanvasLayout,
  type Point,
  type TileRect,
} from "./canvasLayout";
import { blockPaint, containerPaint } from "./blockStyle";
import { matchesQuery, problemOrdinals, type PathNode } from "./canvasModel";
import { controlHash } from "../../app/routes";
import { STATUS_LABELS } from "../security/posture";
import {
  labelOptions,
  markerRows,
  placeFirstFit,
  textWidth,
  type Marker as SecurityMarker,
  type PlacementRequest,
  type Rect,
} from "./securityModel";
import { cloudIconMarkup, cloudIconUrl } from "./cloudAssets";
import {
  cloudProviderOf,
  groupIcon,
  serviceColor,
  serviceIcon,
  type CloudIconRef,
} from "./cloudIcons";
import { iconFor } from "./iconMap";
import {
  COS,
  SIN,
  blockFaces,
  fitView,
  floorTransform,
  inkOn,
  layoutBounds,
  polygonPoints,
  project,
  shade,
  shortLabel,
  zoomAt,
  type View,
  type ViewMode,
} from "./iso";

export type Overlay = "infra" | "path" | "security";
export type TileRef = { envId: string; id: string };

export type PathView = {
  cdc: PathNode[];
  worker: PathNode | null;
  write: PathNode[] | null;
  writing: PathNode | null;
  chip: { jobId: string; stage: string; status: string };
} | null;

const EDGE = 30;
const PLATE_PAD = 6;
const SEV_PAINT: Record<string, { fill: string; stroke: string; ink: string }> =
  {
    critical: { fill: "#fbe9e7", stroke: "#c2362b", ink: "#a02a20" },
    high: { fill: "#fcebdd", stroke: "#d9661f", ink: "#9a4710" },
    medium: { fill: "#f8efd3", stroke: "#b8860b", ink: "#7a5a08" },
    low: { fill: "#f2f2f2", stroke: "#8a8a8a", ink: "#525252" },
  };

function trimSegment(a: Point, b: Point, distance: number): [Point, Point] {
  const length = Math.hypot(b[0] - a[0], b[1] - a[1]);
  if (length <= distance * 2) return [a, b];
  const ratio = distance / length;
  return [pointAlong(a, b, ratio), pointAlong(a, b, 1 - ratio)];
}

function Marker({
  cx,
  cy,
  count,
  paint,
  label,
  plate,
  fs,
  expanded,
  onToggle,
}: {
  cx: number;
  cy: number;
  expanded?: boolean;
  onToggle?: () => void;
  count: number;
  paint: { fill: string; stroke: string; ink: string };
  label: string;
  plate: Rect | null;
  fs: number;
}) {
  return (
    <g
      className={onToggle ? "canvas-marker interactive" : "canvas-marker"}
      role={onToggle ? "button" : undefined}
      tabIndex={onToggle ? 0 : undefined}
      aria-expanded={onToggle ? Boolean(expanded) : undefined}
      aria-label={
        onToggle ? `${label} 상세 ${expanded ? "닫기" : "열기"}` : undefined
      }
      onPointerDown={onToggle ? (event) => event.stopPropagation() : undefined}
      onPointerUp={onToggle ? (event) => event.stopPropagation() : undefined}
      onClick={
        onToggle
          ? (event) => {
              event.stopPropagation();
              onToggle();
            }
          : undefined
      }
      onKeyDown={
        onToggle
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onToggle();
              }
            }
          : undefined
      }
    >
      <title>{label}</title>
      {plate && (
        <g>
          <rect
            x={plate.x}
            y={plate.y}
            width={plate.w}
            height={plate.h}
            rx="5"
            className="marker-plate"
            stroke={paint.stroke}
          />
          <text
            x={plate.x + PLATE_PAD}
            y={plate.y + plate.h / 2 + fs * 0.36}
            className="marker-label"
            fill={paint.ink}
            style={{ fontSize: fs }}
          >
            {label}
          </text>
        </g>
      )}
      <circle
        cx={cx}
        cy={cy}
        r={fs * 0.85}
        fill={paint.fill}
        stroke={paint.stroke}
        strokeWidth="1.5"
      />
      <text
        x={cx}
        y={cy + fs * 0.38}
        textAnchor="middle"
        className="marker-count"
        fill={paint.ink}
        style={{ fontSize: fs }}
      >
        {count}
      </text>
    </g>
  );
}

function Block({
  mode,
  component,
  tile,
  cloudRef,
  muted,
  dim,
  found,
  selected,
  onHover,
  onActivate,
}: {
  mode: ViewMode;
  cloudRef: CloudIconRef | null;
  component: EnvComponent;
  tile: TileRect;
  muted: boolean;
  dim: boolean;
  found: boolean;
  selected: boolean;
  onHover: (id: string | null) => void;
  onActivate: () => void;
}) {
  const [cx, cy] = tileCenter(tile);
  const [px, py] = project(mode, cx, cy);
  const basePaint = blockPaint(component, muted);
  const brand = muted ? null : serviceColor(cloudRef);
  const markup = brand ? cloudIconMarkup(cloudRef) : undefined;
  const cloudUrl =
    !brand && cloudRef && !muted ? cloudIconUrl(cloudRef) : undefined;
  const paint = {
    ...basePaint,
    top: brand ?? (cloudUrl ? "#ffffff" : basePaint.top),
  };
  const faces = blockFaces(mode, EDGE, mode === "iso" ? paint.height : 0);
  const imageSize = EDGE;
  const imageTransform =
    mode === "iso"
      ? `translate(${faces.topCenter[0]} ${faces.topCenter[1]}) matrix(${COS} ${SIN} ${-COS} ${SIN} 0 0)`
      : "translate(0 0)";
  const icon = iconFor(component);
  const ink = inkOn(paint.top);
  const glyphScale = (EDGE * 0.68) / 24;
  const glyphTransform =
    mode === "iso"
      ? `translate(${faces.topCenter[0]} ${faces.topCenter[1]}) matrix(${COS} ${SIN} ${-COS} ${SIN} 0 0) scale(${glyphScale}) translate(-12 -12)`
      : `translate(0 0) scale(${(EDGE * 0.64) / 24}) translate(-12 -12)`;
  return (
    <g
      className={`iso-block${dim ? " dim" : ""}`}
      transform={`translate(${px} ${py})`}
      role="button"
      tabIndex={0}
      aria-label={component.name}
      aria-pressed={selected}
      onPointerEnter={() => onHover(component.id)}
      onPointerLeave={() => onHover(null)}
      onClick={(event) => {
        event.stopPropagation();
        onActivate();
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onActivate();
        }
      }}
    >
      {mode === "iso" ? (
        <>
          <polygon
            points={polygonPoints(faces.left)}
            fill={shade(paint.top, 0.82)}
          />
          <polygon
            points={polygonPoints(faces.right)}
            fill={shade(paint.top, 0.68)}
          />
          <polygon points={polygonPoints(faces.top)} fill={paint.top} />
        </>
      ) : (
        <rect
          x={-EDGE / 2}
          y={-EDGE / 2}
          width={EDGE}
          height={EDGE}
          rx="6"
          fill={paint.top}
          stroke={shade(paint.top, 0.7)}
        />
      )}
      {markup && (
        <g transform={imageTransform}>
          <g
            transform={`translate(${-imageSize / 2} ${-imageSize / 2}) scale(${imageSize / 80})`}
            dangerouslySetInnerHTML={{ __html: markup }}
          />
        </g>
      )}
      {cloudUrl && (
        <image
          href={cloudUrl}
          x={-imageSize / 2}
          y={-imageSize / 2}
          width={imageSize}
          height={imageSize}
          transform={imageTransform}
        />
      )}
      <g
        display={markup || cloudUrl ? "none" : undefined}
        transform={glyphTransform}
        fill={icon.filled ? ink : "none"}
        stroke={icon.filled ? "none" : ink}
        strokeWidth={icon.filled ? 0 : 1.7}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d={icon.path} />
      </g>
      {(selected || found) && (
        <polygon
          points={polygonPoints(faces.silhouette)}
          className={selected ? "block-ring selected" : "block-ring"}
        />
      )}
    </g>
  );
}

export function IsoCanvas({
  layout,
  environments,
  mode,
  onModeChange,
  overlay,
  query,
  selected,
  onSelect,
  markers,
  path,
  height,
}: {
  layout: CanvasLayout;
  environments: FleetEnvironment[];
  mode: ViewMode;
  onModeChange: (mode: ViewMode) => void;
  overlay: Overlay;
  query: string;
  selected: TileRef | null;
  onSelect: (ref: TileRef | null) => void;
  markers: Map<string, SecurityMarker>;
  path: PathView;
  height: number;
}) {
  const { ref, width } = useElementWidth(900);
  const svgRef = useRef<SVGSVGElement>(null);
  const [view, setView] = useState<View>({ x: 0, y: 0, k: 1 });
  const [hover, setHover] = useState<string | null>(null);
  const [openMarker, setOpenMarker] = useState<string | null>(null);
  const pointers = useRef(new Map<number, Point>());
  const drag = useRef({ moved: false, startX: 0, startY: 0 });
  const bounds = layoutBounds(layout, mode);
  const fit = () => fitView(bounds, width, height);

  useEffect(() => {
    setView(fitView(layoutBounds(layout, mode), width, height));
  }, [mode, layout.width, layout.height, width, height]);

  useEffect(() => {
    if (openMarker === null) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpenMarker(null);
    };
    const onDown = (event: PointerEvent) => {
      const target = event.target as Element | null;
      if (!target?.closest(".marker-pop, .canvas-marker.interactive"))
        setOpenMarker(null);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onDown);
    };
  }, [openMarker]);

  useEffect(() => {
    if (overlay !== "security") setOpenMarker(null);
  }, [overlay]);

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const box = svg.getBoundingClientRect();
      const factor = Math.exp(-event.deltaY * (event.ctrlKey ? 0.01 : 0.0015));
      setView((prev) =>
        zoomAt(prev, factor, event.clientX - box.left, event.clientY - box.top),
      );
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, []);

  const onPointerDown = (event: ReactPointerEvent<SVGSVGElement>) => {
    pointers.current.set(event.pointerId, [event.clientX, event.clientY]);
    drag.current = {
      moved: false,
      startX: event.clientX,
      startY: event.clientY,
    };
  };
  const onPointerMove = (event: ReactPointerEvent<SVGSVGElement>) => {
    const previous = pointers.current.get(event.pointerId);
    if (!previous) return;
    const next: Point = [event.clientX, event.clientY];
    if (!drag.current.moved) {
      if (
        Math.hypot(
          next[0] - drag.current.startX,
          next[1] - drag.current.startY,
        ) < 4
      )
        return;
      drag.current.moved = true;
      event.currentTarget.setPointerCapture(event.pointerId);
    }
    const others = [...pointers.current.entries()].filter(
      ([id]) => id !== event.pointerId,
    );
    if (others.length === 1) {
      const other = others[0][1];
      const before = Math.hypot(previous[0] - other[0], previous[1] - other[1]);
      const after = Math.hypot(next[0] - other[0], next[1] - other[1]);
      const box = event.currentTarget.getBoundingClientRect();
      if (before > 0)
        setView((prev) =>
          zoomAt(
            prev,
            after / before,
            (next[0] + other[0]) / 2 - box.left,
            (next[1] + other[1]) / 2 - box.top,
          ),
        );
    } else {
      setView((prev) => ({
        ...prev,
        x: prev.x + next[0] - previous[0],
        y: prev.y + next[1] - previous[1],
      }));
    }
    pointers.current.set(event.pointerId, next);
  };
  const onPointerUp = (event: ReactPointerEvent<SVGSVGElement>) => {
    pointers.current.delete(event.pointerId);
    if (!drag.current.moved) onSelect(null);
  };

  const tileOf = new Map(layout.tiles.map((t) => [`${t.envId}:${t.id}`, t]));
  const screenCenter = (tile: TileRect): Point => {
    const [cx, cy] = tileCenter(tile);
    return project(mode, cx, cy);
  };
  const pathMode = overlay === "path";
  const pathKeys = new Set(
    [...(path?.cdc ?? []), ...(path?.worker ? [path.worker] : [])].map(
      (n) => `${n.envId}:${n.id}`,
    ),
  );
  const writingKey = path?.writing
    ? `${path.writing.envId}:${path.writing.id}`
    : null;
  const flows = path ? routeNodes(path.cdc, layout) : [];
  const writeFlows = path?.write ? routeNodes(path.write, layout) : [];
  const lastFlow = flows[flows.length - 1];
  const chipAt = lastFlow
    ? pointAlong(
        screenCenter(
          tileOf.get(`${lastFlow.from.envId}:${lastFlow.from.id}`) as TileRect,
        ),
        screenCenter(
          tileOf.get(`${lastFlow.to.envId}:${lastFlow.to.id}`) as TileRect,
        ),
        0.62,
      )
    : null;
  const chipText = path ? `${path.chip.jobId} · ${path.chip.status}` : "";
  const active = selected?.id ?? hover;
  const blocks = environments
    .flatMap((env) =>
      env.components.flatMap((component) => {
        const tile = tileOf.get(`${env.id}:${component.id}`);
        return tile ? [{ env, component, tile }] : [];
      }),
    )
    .sort((a, b) => a.tile.x + a.tile.y - (b.tile.x + b.tile.y));
  const searching = query.trim() !== "";
  const grid = 48;
  const fs = Math.max(12, 12 / view.k);
  const lift = (component: EnvComponent) =>
    mode === "iso"
      ? blockPaint(component, false).height + EDGE * SIN + fs * 1.6
      : EDGE / 2 + fs * 1.6;
  const floorFont = Math.max(11, 10 / view.k);
  const floorObstacle = (
    x: number,
    y: number,
    text: string,
    size: number,
  ): Rect[] => {
    const length = textWidth(text, size);
    const steps = Math.max(1, Math.ceil(length / (size * 1.6)));
    return Array.from({ length: steps }, (_, i) => {
      const [px, py] = project(mode, x + (length * i) / steps, y);
      const w = mode === "iso" ? (length / steps) * COS : length / steps;
      const h = mode === "iso" ? (length / steps) * SIN + size * 0.7 : size;
      return { x: px, y: py - size, w, h };
    });
  };
  const obstacles: Rect[] = [
    ...layout.groups.flatMap((g) =>
      floorObstacle(g.x + 8, g.y + 17, g.label, floorFont),
    ),
    ...layout.envHeads.flatMap((h) =>
      floorObstacle(
        h.x,
        h.y + 22,
        environments.find((e) => e.id === h.envId)?.name ?? "",
        Math.max(16, 11 / view.k),
      ),
    ),
    ...blocks.map(({ component, tile }) => {
      const [px, py] = screenCenter(tile);
      const top = mode === "iso" ? blockPaint(component, false).height : 0;
      return {
        x: px - EDGE * (mode === "iso" ? COS : 0.5),
        y: py - top - (mode === "iso" ? EDGE * SIN : EDGE / 2),
        w: EDGE * (mode === "iso" ? 2 * COS : 1),
        h: top + (mode === "iso" ? 2 * EDGE * SIN : EDGE),
      };
    }),
  ];
  const floorCount = obstacles.length - blocks.length;
  const nameText = (name: string, long: boolean) =>
    shortLabel(name, mode === "flat" ? 10 : long ? 16 : 9);
  const nameRequests: PlacementRequest[] = blocks.map(({ component, tile }) => {
    const [px, py] = screenCenter(tile);
    const below = (mode === "iso" ? EDGE * SIN : EDGE / 2) + 3;
    const make = (long: boolean) => {
      const w = textWidth(nameText(component.name, long), fs) + 6;
      return {
        key: `${long ? "long" : "short"}`,
        rect: { x: px - w / 2, y: py + below, w, h: fs * 1.4 },
      };
    };
    const side = (dir: 1 | -1) => {
      const w = textWidth(nameText(component.name, false), fs) + 6;
      const half = (mode === "iso" ? EDGE * COS : EDGE / 2) + 4;
      return {
        key: dir === 1 ? "right" : "left",
        rect: {
          x: dir === 1 ? px + half : px - half - w,
          y: py - fs * 0.7,
          w,
          h: fs * 1.4,
        },
      };
    };
    return {
      id: `${component.id}@${tile.envId}`,
      options: [make(true), make(false), side(1), side(-1)],
    };
  });
  const namePlaced = placeFirstFit(nameRequests, obstacles.slice(floorCount));
  const nameRects = [...namePlaced.values()].flatMap((o) =>
    o ? [o.rect] : [],
  );
  const markerSpots = [...markers.values()].flatMap((m) => {
    if (m.componentId === null) {
      const root = layout.groups.find(
        (g) => g.envId === m.envId && g.depth === 0,
      );
      if (!root) return [];
      const [px, py] = project(mode, root.x, root.y);
      return [{ m, cx: px, cy: py - fs * 1.6 }];
    }
    const tile = tileOf.get(`${m.envId}:${m.componentId}`);
    const component = environments
      .find((e) => e.id === m.envId)
      ?.components.find((c) => c.id === m.componentId);
    if (!tile || !component) return [];
    const [px, py] = screenCenter(tile);
    return [{ m, cx: px, cy: py - lift(component) }];
  });
  const markerText = (m: SecurityMarker) =>
    `${shortLabel(m.title, 14)}${m.extra > 0 ? ` +${m.extra}` : ""}`;
  const circles = markerSpots.map(({ cx, cy }) => ({
    x: cx - fs * 0.85,
    y: cy - fs * 0.85,
    w: fs * 1.7,
    h: fs * 1.7,
  }));
  const markerPlaced = placeFirstFit(
    markerSpots.map(({ m, cx, cy }) => {
      const w = textWidth(markerText(m), fs) + PLATE_PAD * 2;
      return {
        id: m.key,
        options: labelOptions(cx, cy, fs * 0.85, w, fs * 1.8),
      };
    }),
    [...obstacles.slice(0, floorCount), ...nameRects, ...circles],
  );

  return (
    <div className="iso-wrap" ref={ref} style={{ height }}>
      <svg
        ref={svgRef}
        className="iso-svg"
        width={width}
        height={height}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      >
        <defs>
          <pattern
            id="iso-grid"
            width={grid}
            height={grid}
            patternUnits="userSpaceOnUse"
          >
            <path d={`M${grid} 0H0V${grid}`} className="iso-grid-line" />
          </pattern>
          <marker
            id="flow-arrow"
            viewBox="0 0 8 8"
            refX="7"
            refY="4"
            markerWidth="8"
            markerHeight="8"
            orient="auto"
          >
            <path d="M0 0 L8 4 L0 8 z" className="flow-arrow" />
          </marker>
          <marker
            id="write-arrow"
            viewBox="0 0 8 8"
            refX="7"
            refY="4"
            markerWidth="8"
            markerHeight="8"
            orient="auto"
          >
            <path d="M0 0 L8 4 L0 8 z" className="write-arrow" />
          </marker>
        </defs>
        <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
          <g transform={floorTransform(mode)} opacity={pathMode ? 0.4 : 1}>
            <rect
              x={-3000}
              y={-3000}
              width={9000}
              height={9000}
              fill="url(#iso-grid)"
            />
            {layout.envHeads.map((head) => {
              const env = environments.find((e) => e.id === head.envId);
              return (
                <text
                  key={head.envId}
                  x={head.x}
                  y={head.y + 22}
                  className="floor-env"
                  style={{ fontSize: Math.max(16, 11 / view.k) }}
                >
                  {env?.name}
                  <tspan className="floor-env-sub">
                    {"  "}
                    {env?.role === "legacy" ? "레거시" : "신규"}
                  </tspan>
                </text>
              );
            })}
            {[...layout.groups]
              .sort((a, b) => a.depth - b.depth)
              .map((group) => {
                const paint = containerPaint(group);
                return (
                  <g key={group.key}>
                    <rect
                      x={group.x}
                      y={group.y}
                      width={group.w}
                      height={group.h}
                      rx="3"
                      fill={paint.fill}
                      stroke={paint.stroke}
                      strokeWidth="1.3"
                      strokeDasharray={paint.dash}
                    />
                  </g>
                );
              })}
          </g>
          <g className="iso-lines">
            {layout.routes.map((route) => {
              const a = screenCenter(
                tileOf.get(`${route.envId}:${route.from}`) as TileRect,
              );
              const b = screenCenter(
                tileOf.get(`${route.envId}:${route.to}`) as TileRect,
              );
              const lit =
                active !== null &&
                (route.from === active || route.to === active);
              return (
                <line
                  key={`${route.envId}:${route.from}>${route.to}`}
                  x1={a[0]}
                  y1={a[1]}
                  x2={b[0]}
                  y2={b[1]}
                  className={`canvas-line${lit ? " active" : ""}${pathMode ? " muted" : ""}`}
                />
              );
            })}
            {layout.links.map((link) => {
              const a = screenCenter(
                tileOf.get(
                  `${link.route.envId}:${link.route.from}`,
                ) as TileRect,
              );
              const to = layout.tiles.find((t) => t.id === link.route.to);
              const b = screenCenter(to as TileRect);
              const mid = pointAlong(a, b, 0.5);
              const w = link.label.length * fs * 0.55 + 20;
              return (
                <g
                  key={`${link.route.from}~${link.route.to}`}
                  className={pathMode ? "muted-group" : undefined}
                >
                  <line
                    x1={a[0]}
                    y1={a[1]}
                    x2={b[0]}
                    y2={b[1]}
                    className="canvas-line link"
                  />
                  <rect
                    x={mid[0] - w / 2}
                    y={mid[1] - fs * 0.95}
                    width={w}
                    height={fs * 1.9}
                    rx="4"
                    className="label-plate"
                  />
                  <text
                    x={mid[0]}
                    y={mid[1] + fs * 0.35}
                    textAnchor="middle"
                    className="plate-text"
                    style={{ fontSize: fs }}
                  >
                    {link.label}
                  </text>
                </g>
              );
            })}
            {pathMode &&
              flows.map((flow, i) => {
                const [a, b] = trimSegment(
                  screenCenter(
                    tileOf.get(
                      `${flow.from.envId}:${flow.from.id}`,
                    ) as TileRect,
                  ),
                  screenCenter(
                    tileOf.get(`${flow.to.envId}:${flow.to.id}`) as TileRect,
                  ),
                  24,
                );
                return (
                  <line
                    key={`cdc-${i}`}
                    x1={a[0]}
                    y1={a[1]}
                    x2={b[0]}
                    y2={b[1]}
                    className="flow-line"
                    markerEnd="url(#flow-arrow)"
                  />
                );
              })}
            {pathMode &&
              writeFlows.map((flow, i) => {
                const [a, b] = trimSegment(
                  screenCenter(
                    tileOf.get(
                      `${flow.from.envId}:${flow.from.id}`,
                    ) as TileRect,
                  ),
                  screenCenter(
                    tileOf.get(`${flow.to.envId}:${flow.to.id}`) as TileRect,
                  ),
                  24,
                );
                return (
                  <line
                    key={`write-${i}`}
                    x1={a[0] + 4}
                    y1={a[1] + 4}
                    x2={b[0] + 4}
                    y2={b[1] + 4}
                    className="write-line"
                    markerEnd="url(#write-arrow)"
                  />
                );
              })}
          </g>
          {blocks.map(({ env, component, tile }) => {
            const key = `${env.id}:${component.id}`;
            const dim =
              (pathMode && !pathKeys.has(key)) ||
              (searching && !matchesQuery(component, query));
            return (
              <Block
                key={key}
                mode={mode}
                component={component}
                tile={tile}
                cloudRef={serviceIcon(cloudProviderOf(env), component)}
                muted={false}
                dim={dim}
                found={searching && matchesQuery(component, query)}
                selected={
                  selected?.envId === env.id && selected.id === component.id
                }
                onHover={setHover}
                onActivate={() => {
                  if (drag.current.moved) return;
                  onSelect(
                    selected?.envId === env.id && selected.id === component.id
                      ? null
                      : { envId: env.id, id: component.id },
                  );
                }}
              />
            );
          })}
          <g
            transform={floorTransform(mode)}
            opacity={pathMode ? 0.4 : 1}
            className="floor-labels"
          >
            {[...layout.groups]
              .sort((a, b) => a.depth - b.depth)
              .map((group) => {
                const paint = containerPaint(group);
                const env = environments.find((e) => e.id === group.envId);
                const iconUrl = cloudIconUrl(
                  groupIcon(env ? cloudProviderOf(env) : null, group),
                );
                const fontSize = Math.max(11, 10 / view.k);
                const iconSize = Math.round(fontSize * 1.3);
                return (
                  <g key={`label-${group.key}`}>
                    {iconUrl && (
                      <image
                        href={iconUrl}
                        x={group.x + 8}
                        y={group.y + 17 - iconSize + 2}
                        width={iconSize}
                        height={iconSize}
                      />
                    )}
                    <text
                      x={group.x + 8 + (iconUrl ? iconSize + 4 : 0)}
                      y={group.y + 17}
                      className="floor-label"
                      fill={paint.ink}
                      style={{ fontSize }}
                    >
                      {group.label}
                    </text>
                  </g>
                );
              })}
          </g>
          <g className="iso-labels">
            {blocks.map(({ env, component }) => {
              const key = `${env.id}:${component.id}`;
              const placed = namePlaced.get(`${component.id}@${env.id}`);
              if (!placed) return null;
              const dim =
                (pathMode && !pathKeys.has(key)) ||
                (searching && !matchesQuery(component, query));
              const text = nameText(component.name, placed.key === "long");
              return (
                <text
                  key={`label-${key}`}
                  x={placed.rect.x + placed.rect.w / 2}
                  y={placed.rect.y + fs * 1.05}
                  textAnchor="middle"
                  className={`block-label${dim ? " dim" : ""}`}
                  style={{ fontSize: fs }}
                >
                  <title>{component.name}</title>
                  {text}
                </text>
              );
            })}
            {pathMode &&
              writingKey &&
              (() => {
                const tile = tileOf.get(writingKey);
                if (!tile) return null;
                const [px, py] = screenCenter(tile);
                const top =
                  mode === "iso"
                    ? -blockPaint({ kind: "postgres" } as EnvComponent, false)
                        .height -
                      EDGE * SIN -
                      18
                    : -EDGE / 2 - 14;
                return (
                  <g transform={`translate(${px} ${py + top})`}>
                    <rect
                      x={-fs * 2.4}
                      y={-fs * 0.95}
                      width={fs * 4.8}
                      height={fs * 1.9}
                      rx="4"
                      className="writing-plate"
                    />
                    <text
                      textAnchor="middle"
                      y={fs * 0.35}
                      className="writing-text"
                      style={{ fontSize: fs }}
                    >
                      쓰는 중
                    </text>
                  </g>
                );
              })()}
            {pathMode && chipAt && (
              <g transform={`translate(${chipAt[0]} ${chipAt[1]})`}>
                <rect
                  x={-(chipText.length * fs * 0.58 + 20) / 2}
                  y={-fs}
                  width={chipText.length * fs * 0.58 + 20}
                  height={fs * 2}
                  rx="4"
                  className="chip-plate"
                />
                <text
                  textAnchor="middle"
                  y={fs * 0.35}
                  className="plate-text"
                  style={{ fontSize: fs }}
                >
                  {chipText}
                </text>
              </g>
            )}
            {overlay === "security" &&
              markerSpots.map(({ m, cx, cy }) => {
                const spot = markerPlaced.get(m.key);
                return (
                  <Marker
                    key={m.key}
                    cx={cx}
                    cy={cy}
                    count={m.controls.length}
                    paint={SEV_PAINT[m.severity]}
                    label={markerText(m)}
                    plate={spot ? spot.rect : null}
                    fs={fs}
                    expanded={openMarker === m.key}
                    onToggle={() =>
                      setOpenMarker((prev) => (prev === m.key ? null : m.key))
                    }
                  />
                );
              })}
            {overlay === "infra" &&
              blocks.flatMap(({ env, component, tile }) => {
                const ordinal = problemOrdinals(env.components).get(
                  component.id,
                );
                if (ordinal === undefined) return [];
                const [px, py] = screenCenter(tile);
                const cy = py - lift(component);
                const label = shortLabel(component.name, 16);
                const w = textWidth(label, fs) + PLATE_PAD * 2;
                return [
                  <Marker
                    key={`prob-${env.id}:${component.id}`}
                    cx={px}
                    cy={cy}
                    count={ordinal}
                    paint={
                      SEV_PAINT[
                        component.status === "down" ? "critical" : "medium"
                      ]
                    }
                    label={label}
                    plate={{
                      x: px + fs * 1.1,
                      y: cy - fs * 0.9,
                      w,
                      h: fs * 1.8,
                    }}
                    fs={fs}
                  />,
                ];
              })}
          </g>
        </g>
      </svg>
      {overlay === "security" &&
        openMarker !== null &&
        (() => {
          const spot = markerSpots.find((s) => s.m.key === openMarker);
          if (!spot) return null;
          const left = Math.min(
            Math.max(8, view.x + spot.cx * view.k - 16),
            Math.max(8, width - 320),
          );
          const top = view.y + spot.cy * view.k + 16;
          return (
            <div
              className="marker-pop"
              role="dialog"
              aria-label="이 위치의 보안 문제"
              style={{ left, top }}
            >
              <ul>
                {markerRows(spot.m).map((row) => (
                  <li key={row.id}>
                    <span className="marker-pop-title">
                      <i
                        className={`dot sev-dot-${row.severity}`}
                        aria-hidden="true"
                      />
                      {row.title}
                    </span>
                    <span className="marker-pop-meta">
                      {STATUS_LABELS[row.status]}
                      <a href={controlHash(row.id)}>자세히 보기 →</a>
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          );
        })()}
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
