import { useEffect, useRef, useState } from "react";
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
import { componentTone } from "./model";
import { podCells, podSummary } from "./podModel";
import { primaryEndpoint } from "./endpointModel";
import { matchesQuery, problemOrdinals, type PathNode } from "./canvasModel";
import { controlHash } from "../../app/routes";
import { CATEGORY_LABELS, SEVERITY_ORDER } from "../security/posture";
import { CategoryIcon } from "../security/CategoryIcon";
import { SeverityMeter } from "../security/SeverityMeter";
import {
  labelOptions,
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
import { usePanZoom } from "./usePanZoom";
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
    critical: { fill: "#fcebea", stroke: "#c9302d", ink: "#b32322" },
    high: { fill: "#fdeee3", stroke: "#dd6f23", ink: "#a64d10" },
    medium: { fill: "#f3f4fd", stroke: "#8b95a1", ink: "#6b7684" },
    low: { fill: "#f3f4fd", stroke: "#b0b8c1", ink: "#6b7684" },
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
  onActivate: (pointer: boolean) => void;
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
        onActivate(true);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onActivate(false);
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
  height: maxHeight,
  showPods = false,
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
  /** 파드 보기(Cloudcraft 처럼 기본은 끔): 워크로드 타일 뒤쪽에 파드 하나당 작은 블록 하나 */
  showPods?: boolean;
}) {
  const { ref, width } = useElementWidth(900);
  const svgRef = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [openMarker, setOpenMarker] = useState<string | null>(null);
  const { view, setView, moved, handlers } = usePanZoom(svgRef, () =>
    onSelect(null),
  );
  const bounds = layoutBounds(layout, mode);
  // 캔버스 높이는 그림 비율에 맞춘다(너비에 맞춰 그렸을 때 높이, 최소 480). 큰 빈 공간을 두지 않는다.
  const fitScale = Math.min(
    (width - 48) / Math.max(bounds.maxX - bounds.minX, 1),
    1.4,
  );
  const height = Math.round(
    Math.min(
      maxHeight,
      Math.max(480, (bounds.maxY - bounds.minY) * fitScale + 72),
    ),
  );
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
  const chipText = path ? `${path.chip.jobId}, ${path.chip.status}` : "";
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
  // 화면에서 늘 12px 이상으로 보이게 한다. 겹침은 이름 배치(블록)와 너비 맞춤(그룹)으로 막는다.
  const fs = Math.max(12, 12 / view.k);
  const lift = (component: EnvComponent) =>
    mode === "iso"
      ? blockPaint(component, false).height + EDGE * SIN + fs * 1.6
      : EDGE / 2 + fs * 1.6;
  // 바닥 글자(그룹·환경 이름)는 머리 공간(GROUP_HEAD·ENV_HEAD) 안에 들어가는 크기까지만 키운다.
  // 파드 칸: 블록 오른쪽에 5칸씩 최대 3줄(Cloudcraft 가 인스턴스를 나란히 그리듯). 파드 하나 = 작은 블록 하나.
  const POD_EDGE = 9;
  const POD_COLS = 5;
  const POD_ROWS = 3;
  const podLayout = (tile: TileRect, component: EnvComponent) => {
    if (!showPods) return null;
    const { cells, hidden } = podCells(component, POD_COLS * POD_ROWS);
    if (cells.length === 0) return null;
    const [px, py] = screenCenter(tile);
    const startX = px + (mode === "iso" ? EDGE * COS : EDGE / 2) + 8;
    const step = mode === "iso" ? 11 : 12;
    const rowStep = mode === "iso" ? 10 : 12;
    const rows = Math.ceil(cells.length / POD_COLS);
    const top = py - ((rows - 1) * rowStep) / 2;
    const spots = cells.map((cell, i) => ({
      ...cell,
      x: startX + (i % POD_COLS) * step,
      y: top + Math.floor(i / POD_COLS) * rowStep,
    }));
    const last = spots[spots.length - 1];
    const more: Point | null = hidden ? [last.x + step - 2, last.y] : null;
    return { spots, hidden, more };
  };
  // 화면에서 그룹 이름 12px, 환경 이름 16px 이상(머리 공간 GROUP_HEAD 46, ENV_HEAD 48 안).
  const floorFont = Math.min(30, Math.max(12, 12.5 / view.k));
  const envFont = Math.min(34, Math.max(15, 16 / view.k));
  const floorBase = (y: number) => y + 6 + floorFont * 0.82;
  const envBase = (y: number) => y + 6 + envFont * 0.82;
  // 축소해서 볼 때는 네임스페이스 이름을 숨긴다(Cloudcraft 처럼 확대하면 보임). 블록 이름이 우선.
  const showGroupLabel = (group: { kind: string }) =>
    group.kind !== "namespace" || view.k >= 0.7;
  /** 바닥 글자를 주어진 너비에 맞게 자른다(전체 이름은 title 로). */
  const fitText = (text: string, max: number, size: number) => {
    if (textWidth(text, size) <= max) return text;
    let out = text;
    while (out.length > 1 && textWidth(`${out}…`, size) > max)
      out = out.slice(0, -1);
    return `${out}…`;
  };
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
  // 연결선 이름표: 겹치면 아래로 비켜 놓고, 블록 이름 배치의 장애물로도 쓴다.
  const linkPlates: {
    key: string;
    a: Point;
    b: Point;
    mid: Point;
    w: number;
    label: string;
  }[] = [];
  for (const link of layout.links) {
    const fromTile = tileOf.get(`${link.route.envId}:${link.route.from}`);
    const toTile = layout.tiles.find((t) => t.id === link.route.to);
    if (!fromTile || !toTile) continue;
    const a = screenCenter(fromTile);
    const b = screenCenter(toTile);
    const mid = pointAlong(a, b, 0.5);
    const w = textWidth(link.label, fs) + 20;
    let y = mid[1];
    const overlaps = (yy: number) =>
      linkPlates.some(
        (o) =>
          Math.abs(o.mid[0] - mid[0]) < (o.w + w) / 2 &&
          Math.abs(o.mid[1] - yy) < fs * 2,
      );
    while (overlaps(y)) y += fs * 2.1;
    linkPlates.push({
      key: `${link.route.from}~${link.route.to}`,
      a,
      b,
      mid: [mid[0], y],
      w,
      label: link.label,
    });
  }
  const plateRects: Rect[] = linkPlates.map((pl) => ({
    x: pl.mid[0] - pl.w / 2,
    y: pl.mid[1] - fs * 0.95,
    w: pl.w,
    h: fs * 1.9,
  }));
  // 인프라 보기의 문제 표식(번호 + 이름): 이름표가 피해야 할 자리, 표식이 있는 블록은 이름표를 따로 달지 않는다.
  const problemRects: Rect[] =
    overlay === "infra"
      ? blocks.flatMap(({ env, component, tile }) => {
          if (!problemOrdinals(env.components).has(component.id)) return [];
          const [px, py] = screenCenter(tile);
          const cy = py - lift(component);
          const w =
            textWidth(shortLabel(component.name, 16), fs) + PLATE_PAD * 2;
          return [
            { x: px - fs * 0.9, y: cy - fs * 0.9, w: w + fs * 2, h: fs * 1.8 },
          ];
        })
      : [];
  const podRects: Rect[] = showPods
    ? blocks.flatMap(({ component, tile }) => {
        const pods = podLayout(tile, component);
        if (!pods) return [];
        const xs = pods.spots.map((p) => p.x);
        const ys = pods.spots.map((p) => p.y);
        return [
          {
            x: Math.min(...xs) - 8,
            y: Math.min(...ys) - 12,
            w: Math.max(...xs) - Math.min(...xs) + 16 + (pods.more ? 24 : 0),
            h: Math.max(...ys) - Math.min(...ys) + 18,
          },
        ];
      })
    : [];
  const hasProblemMark = (envId: string, id: string) =>
    overlay === "infra" &&
    problemOrdinals(
      environments.find((e) => e.id === envId)?.components ?? [],
    ).has(id);
  const obstacles: Rect[] = [
    ...layout.groups
      .filter(showGroupLabel)
      .flatMap((g) =>
        floorObstacle(
          g.x + 8,
          floorBase(g.y),
          fitText(g.label, g.w - 16 - floorFont * 1.6, floorFont),
          floorFont,
        ),
      ),
    ...layout.envHeads.flatMap((h) =>
      floorObstacle(
        h.x,
        envBase(h.y),
        environments.find((e) => e.id === h.envId)?.name ?? "",
        envFont,
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
    ...plateRects,
    ...problemRects,
    ...podRects,
  ];
  const floorCount =
    obstacles.length -
    blocks.length -
    plateRects.length -
    problemRects.length -
    podRects.length;
  type NameSize = "long" | "short" | "tiny";
  const NAME_CHARS: Record<NameSize, number> = { long: 24, short: 13, tiny: 8 };
  const nameText = (name: string, size: NameSize) =>
    shortLabel(
      name,
      mode === "flat" ? Math.min(14, NAME_CHARS[size]) : NAME_CHARS[size],
    );
  // 이름 뒤 파드 수(준비/전체). 파드가 2개 이상이거나 준비 안 된 파드가 있을 때만.
  const countText = (component: EnvComponent) => {
    const s = podSummary(component);
    if (!s || (s.total <= 1 && s.ready === s.total)) return "";
    return `${s.ready}/${s.total}`;
  };
  // 이름 앞 상태 점(Cloudcraft 처럼 "● 이름") 자리
  const dotRoom = fs * 0.95;
  const nameRequests: PlacementRequest[] = blocks
    .filter(({ env, component }) => !hasProblemMark(env.id, component.id))
    .map(({ component, tile }) => {
      const [px, py] = screenCenter(tile);
      const below = (mode === "iso" ? EDGE * SIN : EDGE / 2) + 3;
      const half = (mode === "iso" ? EDGE * COS : EDGE / 2) + 4;
      const top =
        mode === "iso"
          ? blockPaint(component, false).height + EDGE * SIN
          : EDGE / 2;
      const count = countText(component);
      const at = (pos: "below" | "right" | "left" | "up", size: NameSize) => {
        const w =
          textWidth(nameText(component.name, size), fs) +
          6 +
          dotRoom +
          (count ? textWidth(count, fs * 0.9) + fs * 0.4 : 0) +
          (primaryEndpoint(component) ? fs * 1.1 : 0);
        const h = fs * 1.4;
        const rect =
          pos === "below"
            ? { x: px - w / 2, y: py + below, w, h }
            : pos === "right"
              ? { x: px + half, y: py - fs * 0.7, w, h }
              : pos === "left"
                ? { x: px - half - w, y: py - fs * 0.7, w, h }
                : { x: px - w / 2, y: py - top - fs * 1.6, w, h };
        return { key: `${pos}:${size}`, rect };
      };
      // 긴 이름 → 보통 이름(아래, 오른쪽, 왼쪽, 위) → 짧은 이름. 자리가 좁아도 이름을 빼먹지 않는다.
      return {
        id: `${component.id}@${tile.envId}`,
        options: [
          at("below", "long"),
          at("below", "short"),
          at("right", "short"),
          at("left", "short"),
          at("up", "short"),
          at("below", "tiny"),
          at("right", "tiny"),
          at("left", "tiny"),
          at("up", "tiny"),
        ],
      };
    });
  // 먼저 그룹 이름까지 피해서 놓고, 자리가 없을 때만 그룹 이름 위에 놓는다(이름을 빼먹지 않는다).
  const strictNames = placeFirstFit(nameRequests, obstacles);
  const strictRects = [...strictNames.values()].flatMap((o) =>
    o ? [o.rect] : [],
  );
  const relaxedNames = placeFirstFit(
    nameRequests.filter((r) => !strictNames.get(r.id)),
    [...obstacles.slice(floorCount), ...strictRects],
  );
  const namePlaced = new Map(
    nameRequests.map((r) => [
      r.id,
      strictNames.get(r.id) ?? relaxedNames.get(r.id) ?? null,
    ]),
  );
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
        {...handlers}
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
                  y={envBase(head.y)}
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
            {linkPlates.map((plate) => (
              <g
                key={plate.key}
                className={pathMode ? "muted-group" : undefined}
              >
                <line
                  x1={plate.a[0]}
                  y1={plate.a[1]}
                  x2={plate.b[0]}
                  y2={plate.b[1]}
                  className="canvas-line link"
                />
                <rect
                  x={plate.mid[0] - plate.w / 2}
                  y={plate.mid[1] - fs * 0.95}
                  width={plate.w}
                  height={fs * 1.9}
                  rx="4"
                  className="label-plate"
                />
                <text
                  x={plate.mid[0]}
                  y={plate.mid[1] + fs * 0.35}
                  textAnchor="middle"
                  className="plate-text"
                  style={{ fontSize: fs }}
                >
                  {plate.label}
                </text>
              </g>
            ))}
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
          {showPods && (
            <g className="pod-layer">
              {blocks.map(({ env, component, tile }) => {
                const pods = podLayout(tile, component);
                if (!pods) return null;
                const key = `${env.id}:${component.id}`;
                const dim =
                  (pathMode && !pathKeys.has(key)) ||
                  (searching && !matchesQuery(component, query));
                const faces = blockFaces(
                  mode,
                  POD_EDGE,
                  mode === "iso" ? 7 : 0,
                );
                return (
                  <g key={`pods-${key}`} opacity={dim ? 0.25 : 1}>
                    {pods.spots.map((spot) => (
                      <g
                        key={spot.key}
                        className={`pod-cube tone-${spot.tone}`}
                        transform={`translate(${spot.x} ${spot.y})`}
                      >
                        <title>{spot.title}</title>
                        {mode === "iso" ? (
                          <>
                            <polygon
                              className="pc-l"
                              points={polygonPoints(faces.left)}
                            />
                            <polygon
                              className="pc-r"
                              points={polygonPoints(faces.right)}
                            />
                            <polygon
                              className="pc-t"
                              points={polygonPoints(faces.top)}
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
                      </g>
                    ))}
                    {pods.more && (
                      <text
                        x={pods.more[0]}
                        y={pods.more[1] + 3}
                        className="pod-more"
                        style={{ fontSize: Math.max(9, 10 / view.k) }}
                      >
                        +{pods.hidden}
                      </text>
                    )}
                  </g>
                );
              })}
            </g>
          )}
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
                onActivate={(pointer) => {
                  if (pointer && moved()) return;
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
              .filter(showGroupLabel)
              .sort((a, b) => a.depth - b.depth)
              .map((group) => {
                const paint = containerPaint(group);
                const env = environments.find((e) => e.id === group.envId);
                const iconUrl = cloudIconUrl(
                  groupIcon(env ? cloudProviderOf(env) : null, group),
                );
                const fontSize = floorFont;
                const iconSize = Math.round(fontSize * 1.3);
                const room = group.w - 16 - (iconUrl ? iconSize + 4 : 0);
                return (
                  <g key={`label-${group.key}`}>
                    {iconUrl && (
                      <image
                        href={iconUrl}
                        x={group.x + 8}
                        y={floorBase(group.y) - iconSize + 2}
                        width={iconSize}
                        height={iconSize}
                      />
                    )}
                    <text
                      x={group.x + 8 + (iconUrl ? iconSize + 4 : 0)}
                      y={floorBase(group.y)}
                      className="floor-label"
                      fill={paint.ink}
                      style={{ fontSize }}
                    >
                      <title>{group.label}</title>
                      {fitText(group.label, room, fontSize)}
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
              const text = nameText(
                component.name,
                placed.key.split(":")[1] as NameSize,
              );
              const tone = componentTone(component.status);
              return (
                <g
                  key={`label-${key}`}
                  className={`block-label-g${dim ? " dim" : ""}`}
                >
                  <circle
                    cx={placed.rect.x + 3 + fs * 0.35}
                    cy={placed.rect.y + fs * 0.7}
                    r={fs * 0.3}
                    className={`label-dot tone-${tone}`}
                  />
                  <text
                    x={placed.rect.x + 3 + dotRoom}
                    y={placed.rect.y + fs * 1.05}
                    className="block-label"
                    style={{ fontSize: fs }}
                  >
                    <title>{component.name}</title>
                    {text}
                    {countText(component) && (
                      <tspan
                        dx={fs * 0.4}
                        className={`label-count${
                          (podSummary(component)?.ready ?? 0) <
                          (podSummary(component)?.total ?? 0)
                            ? " short"
                            : ""
                        }`}
                        style={{ fontSize: fs * 0.9 }}
                      >
                        {countText(component)}
                      </tspan>
                    )}
                    {primaryEndpoint(component) && (
                      <tspan
                        dx={fs * 0.3}
                        className="label-ext"
                        style={{ fontSize: fs * 0.9 }}
                      >
                        ↗
                      </tspan>
                    )}
                  </text>
                </g>
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
          const POP_W = 380;
          const rows = [...spot.m.controls].sort(
            (x, y) =>
              SEVERITY_ORDER.indexOf(x.severity) -
              SEVERITY_ORDER.indexOf(y.severity),
          );
          const env = environments.find((e) => e.id === spot.m.envId);
          const component = env?.components.find(
            (c) => c.id === spot.m.componentId,
          );
          const estimate = 120 + Math.min(rows.length, 5) * 58;
          const anchorY = view.y + spot.cy * view.k;
          const left = Math.min(
            Math.max(8, view.x + spot.cx * view.k - 24),
            Math.max(8, width - POP_W - 8),
          );
          const top =
            anchorY + 16 + estimate > height && anchorY - 16 - estimate > 0
              ? anchorY - 16 - estimate
              : Math.min(anchorY + 16, Math.max(8, height - estimate - 8));
          return (
            <div
              className="marker-pop"
              role="dialog"
              aria-label={`${component?.name ?? env?.name ?? ""} 실패한 통제`}
              style={{ left, top, width: POP_W }}
            >
              <header className="mp-head">
                <div>
                  <h3>{component?.name ?? env?.name}</h3>
                  <p>
                    실패한 통제 <b>{rows.length}</b>
                    {component ? `, ${env?.name ?? ""}` : ", 환경 전체 설정"}
                  </p>
                </div>
                <button
                  type="button"
                  className="mp-close"
                  aria-label="닫기"
                  onClick={() => setOpenMarker(null)}
                >
                  <svg viewBox="0 0 16 16" aria-hidden="true">
                    <path d="M4 4l8 8M12 4l-8 8" />
                  </svg>
                </button>
              </header>
              <ul className="mp-rows">
                {rows.map((control) => (
                  <li key={control.id}>
                    <a href={controlHash(control.id)}>
                      <CategoryIcon category={control.category} />
                      <span className="rule-text">
                        <span className="rule-title">{control.title}</span>
                        <span className="rule-sub">
                          {control.id}, {CATEGORY_LABELS[control.category]}
                        </span>
                      </span>
                      <SeverityMeter severity={control.severity} />
                    </a>
                  </li>
                ))}
              </ul>
              <footer className="mp-foot">
                {component && env ? (
                  <button
                    type="button"
                    className="pk-button weak"
                    onClick={() => {
                      setOpenMarker(null);
                      onSelect({ envId: env.id, id: component.id });
                    }}
                  >
                    구성 요소 상세 열기
                  </button>
                ) : (
                  <span />
                )}
                <a className="text-link" href="#security">
                  보안에서 모두 보기
                </a>
              </footer>
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
