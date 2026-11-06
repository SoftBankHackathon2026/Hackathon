import type { EnvComponent, FleetEnvironment } from "../../api/migrations.ts";
import type { Category, Control, Severity } from "../../api/security.ts";

export type OverlayGroup =
  "misconfig" | "identity" | "detection" | "governance";

export const OVERLAY_GROUPS: { id: OverlayGroup; label: string }[] = [
  { id: "misconfig", label: "설정 오류" },
  { id: "identity", label: "권한 위험" },
  { id: "detection", label: "탐지/감사" },
  { id: "governance", label: "거버넌스" },
];

export function overlayGroupOf(category: Category): OverlayGroup {
  switch (category) {
    case "identity":
      return "identity";
    case "detection":
      return "detection";
    case "governance":
      return "governance";
    default:
      return "misconfig";
  }
}

const SEVERITY_RANK: Severity[] = ["critical", "high", "medium", "low"];

export function bySeverity(a: Control, b: Control) {
  return (
    SEVERITY_RANK.indexOf(a.severity) - SEVERITY_RANK.indexOf(b.severity) ||
    a.id.localeCompare(b.id)
  );
}

export type Marker = {
  key: string;
  envId: string;
  componentId: string | null;
  controls: Control[];
  severity: Severity;
  title: string;
  extra: number;
};

export function failingFor(env: FleetEnvironment, controls: Control[]) {
  return controls.filter((c) => c.results[env.id]?.status === "fail");
}

export function buildMarkers(
  env: FleetEnvironment,
  controls: Control[],
  filter: { group: OverlayGroup | null; hidden: Severity[] },
): Map<string, Marker> {
  const buckets = new Map<
    string,
    { componentId: string | null; list: Control[] }
  >();
  const add = (key: string, componentId: string | null, control: Control) => {
    const bucket = buckets.get(key) ?? { componentId, list: [] };
    bucket.list.push(control);
    buckets.set(key, bucket);
  };
  const byName = new Map<string, EnvComponent>(
    env.components.map((c) => [c.name, c]),
  );
  for (const control of failingFor(env, controls)) {
    if (filter.hidden.includes(control.severity)) continue;
    if (filter.group && overlayGroupOf(control.category) !== filter.group)
      continue;
    const resources = control.results[env.id]?.resources ?? [];
    const tiles = resources.flatMap((name) => {
      const found = byName.get(name);
      return found ? [found] : [];
    });
    if (control.scope === "environment" || tiles.length === 0)
      add(`env:${env.id}`, null, control);
    else
      for (const tile of tiles)
        add(`tile:${env.id}:${tile.id}`, tile.id, control);
  }
  const markers = new Map<string, Marker>();
  for (const [key, bucket] of buckets) {
    const sorted = [...bucket.list].sort(bySeverity);
    markers.set(key, {
      key,
      envId: env.id,
      componentId: bucket.componentId,
      controls: sorted,
      severity: sorted[0].severity,
      title: sorted[0].title,
      extra: sorted.length - 1,
    });
  }
  return markers;
}

export function groupCounts(
  environments: FleetEnvironment[],
  controls: Control[],
) {
  const counts: Record<OverlayGroup, number> = {
    misconfig: 0,
    identity: 0,
    detection: 0,
    governance: 0,
  };
  for (const env of environments)
    for (const control of failingFor(env, controls))
      counts[overlayGroupOf(control.category)] += 1;
  return counts;
}

export function severityCountsFor(
  environments: FleetEnvironment[],
  controls: Control[],
  group: OverlayGroup | null,
) {
  const counts: Record<Severity, number> = {
    critical: 0,
    high: 0,
    medium: 0,
    low: 0,
  };
  for (const env of environments)
    for (const control of failingFor(env, controls))
      if (!group || overlayGroupOf(control.category) === group)
        counts[control.severity] += 1;
  return counts;
}

export type Rect = { x: number; y: number; w: number; h: number };
export type PlacementOption = { key: string; rect: Rect };
export type PlacementRequest = { id: string; options: PlacementOption[] };

export function overlaps(a: Rect, b: Rect, pad = 2) {
  return !(
    a.x + a.w + pad <= b.x ||
    b.x + b.w + pad <= a.x ||
    a.y + a.h + pad <= b.y ||
    b.y + b.h + pad <= a.y
  );
}

export function placeFirstFit(requests: PlacementRequest[], obstacles: Rect[]) {
  const taken = [...obstacles];
  const result = new Map<string, PlacementOption | null>();
  for (const request of requests) {
    const fit = request.options.find(
      (o) => !taken.some((t) => overlaps(o.rect, t)),
    );
    if (fit) taken.push(fit.rect);
    result.set(request.id, fit ?? null);
  }
  return result;
}

export function textWidth(text: string, fontSize: number) {
  let units = 0;
  for (const ch of text) units += ch.charCodeAt(0) > 0x2e80 ? 1 : 0.58;
  return units * fontSize;
}

export function labelOptions(
  cx: number,
  cy: number,
  r: number,
  w: number,
  h: number,
): PlacementOption[] {
  return [
    { key: "right", rect: { x: cx + r + 4, y: cy - h / 2, w, h } },
    { key: "left", rect: { x: cx - r - 4 - w, y: cy - h / 2, w, h } },
    { key: "up", rect: { x: cx - w / 2, y: cy - r - 4 - h, w, h } },
  ];
}
