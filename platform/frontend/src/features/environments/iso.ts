import type { CanvasLayout, Point } from "./canvasLayout.ts";

export type ViewMode = "iso" | "flat";

export const COS = Math.sqrt(3) / 2;
export const SIN = 0.5;

export function project(mode: ViewMode, x: number, y: number): Point {
  return mode === "iso" ? [(x - y) * COS, (x + y) * SIN] : [x, y];
}

export function floorTransform(mode: ViewMode) {
  return mode === "iso"
    ? `matrix(${COS} ${SIN} ${-COS} ${SIN} 0 0)`
    : undefined;
}

export type Faces = {
  top: Point[];
  left: Point[];
  right: Point[];
  silhouette: Point[];
  topCenter: Point;
};

export function blockFaces(
  mode: ViewMode,
  edge: number,
  height: number,
): Faces {
  const half = edge / 2;
  if (mode === "flat") {
    const square: Point[] = [
      [-half, -half],
      [half, -half],
      [half, half],
      [-half, half],
    ];
    return {
      top: square,
      left: [],
      right: [],
      silhouette: square,
      topCenter: [0, 0],
    };
  }
  const n: Point = [0, -edge * SIN];
  const e: Point = [edge * COS, 0];
  const s: Point = [0, edge * SIN];
  const w: Point = [-edge * COS, 0];
  const lift = (p: Point): Point => [p[0], p[1] - height];
  return {
    top: [lift(n), lift(e), lift(s), lift(w)],
    left: [w, s, lift(s), lift(w)],
    right: [s, e, lift(e), lift(s)],
    silhouette: [lift(n), lift(e), e, s, w, lift(w)],
    topCenter: [0, -height],
  };
}

export function polygonPoints(points: Point[]) {
  return points.map((p) => `${round(p[0])},${round(p[1])}`).join(" ");
}

function round(value: number) {
  return Math.round(value * 100) / 100;
}

export function layoutBounds(layout: CanvasLayout, mode: ViewMode, lift = 56) {
  const corners: Point[] = layout.groups.flatMap((g) => [
    project(mode, g.x, g.y),
    project(mode, g.x + g.w, g.y),
    project(mode, g.x, g.y + g.h),
    project(mode, g.x + g.w, g.y + g.h),
  ]);
  const xs = corners.map((c) => c[0]);
  const ys = corners.map((c) => c[1]);
  return {
    minX: Math.min(...xs),
    maxX: Math.max(...xs),
    minY: Math.min(...ys) - lift,
    maxY: Math.max(...ys),
  };
}

export type View = { x: number; y: number; k: number };

export function fitView(
  bounds: { minX: number; maxX: number; minY: number; maxY: number },
  width: number,
  height: number,
  padding = 24,
  maxScale = 1.4,
): View {
  const w = Math.max(bounds.maxX - bounds.minX, 1);
  const h = Math.max(bounds.maxY - bounds.minY, 1);
  const k = Math.min(
    (width - 2 * padding) / w,
    (height - 2 * padding) / h,
    maxScale,
  );
  return {
    k,
    x: (width - w * k) / 2 - bounds.minX * k,
    y: (height - h * k) / 2 - bounds.minY * k,
  };
}

export function zoomAt(
  view: View,
  factor: number,
  cx: number,
  cy: number,
): View {
  const k = Math.min(3, Math.max(0.2, view.k * factor));
  const ratio = k / view.k;
  return { k, x: cx - (cx - view.x) * ratio, y: cy - (cy - view.y) * ratio };
}

export function shade(hex: string, factor: number) {
  const value = hex.replace("#", "");
  const channel = (i: number) => parseInt(value.slice(i, i + 2), 16);
  const mix = (c: number) =>
    Math.max(0, Math.min(255, Math.round(c * factor)))
      .toString(16)
      .padStart(2, "0");
  return `#${mix(channel(0))}${mix(channel(2))}${mix(channel(4))}`;
}

export function inkOn(hex: string) {
  const value = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(value.slice(i, i + 2), 16));
  return 0.299 * r + 0.587 * g + 0.114 * b > 170 ? "#3a3a3a" : "#ffffff";
}

export function shortLabel(text: string, max = 16) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}
