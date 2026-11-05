import type {
  Category,
  Control,
  ControlStatus,
  Severity,
} from "../../api/security.ts";
import { bySeverity } from "../environments/securityModel.ts";
import { standardId } from "./posture.ts";

export type ExplorerFilters = {
  severity: Severity[];
  status: ControlStatus[];
  category: Category[];
  env: string[];
  standard: string[];
  query: string;
};

export const ALL_SEVERITIES: Severity[] = ["critical", "high", "medium", "low"];
export const ALL_CATEGORIES: Category[] = [
  "network",
  "workload",
  "identity",
  "data",
  "detection",
  "governance",
];
export const SHOWN_STATUSES: ControlStatus[] = ["fail", "unknown", "pass"];

export function defaultFilters(
  envIds: string[],
  standardIds: string[],
): ExplorerFilters {
  return {
    severity: [...ALL_SEVERITIES],
    status: ["fail"],
    category: [...ALL_CATEGORIES],
    env: [...envIds],
    standard: [...standardIds],
    query: "",
  };
}

type Dimension = keyof ExplorerFilters;

export function matchesFilters(
  control: Control,
  filters: ExplorerFilters,
  skip?: Dimension,
) {
  if (skip !== "severity" && !filters.severity.includes(control.severity))
    return false;
  if (skip !== "category" && !filters.category.includes(control.category))
    return false;
  if (
    skip !== "standard" &&
    !control.standard_refs.some((ref) =>
      filters.standard.includes(standardId(ref)),
    )
  )
    return false;
  const query = filters.query.trim().toLowerCase();
  if (
    skip !== "query" &&
    query &&
    !`${control.id} ${control.title} ${control.why ?? ""}`
      .toLowerCase()
      .includes(query)
  )
    return false;
  const envs = skip === "env" ? Object.keys(control.results) : filters.env;
  return envs.some((envId) => {
    const status = control.results[envId]?.status;
    if (!status) return false;
    return skip === "status" || filters.status.includes(status);
  });
}

export function filterFindings(controls: Control[], filters: ExplorerFilters) {
  return controls.filter((c) => matchesFilters(c, filters)).sort(bySeverity);
}

export function facetCounts(controls: Control[], filters: ExplorerFilters) {
  const pool = (skip: Dimension) =>
    controls.filter((c) => matchesFilters(c, filters, skip));
  const severity = Object.fromEntries(
    ALL_SEVERITIES.map((s) => [
      s,
      pool("severity").filter((c) => c.severity === s).length,
    ]),
  ) as Record<Severity, number>;
  const category = Object.fromEntries(
    ALL_CATEGORIES.map((k) => [
      k,
      pool("category").filter((c) => c.category === k).length,
    ]),
  ) as Record<Category, number>;
  const status = Object.fromEntries(
    SHOWN_STATUSES.map((st) => [
      st,
      pool("status").filter((c) =>
        filters.env.some((e) => c.results[e]?.status === st),
      ).length,
    ]),
  ) as Record<ControlStatus, number>;
  const envIds = new Set(controls.flatMap((c) => Object.keys(c.results)));
  const env: Record<string, number> = {};
  for (const id of envIds)
    env[id] = pool("env").filter((c) =>
      filters.status.includes(c.results[id]?.status as ControlStatus),
    ).length;
  const standard: Record<string, number> = {};
  for (const c of pool("standard"))
    for (const id of new Set(c.standard_refs.map(standardId)))
      standard[id] = (standard[id] ?? 0) + 1;
  return { severity, category, status, env, standard };
}

export function toggle<T>(list: T[], value: T) {
  return list.includes(value)
    ? list.filter((v) => v !== value)
    : [...list, value];
}

export function affected(control: Control, filters: ExplorerFilters) {
  const envs = filters.env.filter((e) => {
    const status = control.results[e]?.status;
    return status !== undefined && filters.status.includes(status);
  });
  const resources = new Set(
    envs.flatMap((e) => control.results[e]?.resources ?? []),
  );
  return {
    envs,
    resources: control.scope === "environment" ? 0 : resources.size,
  };
}

export function excerpt(text: string | undefined, max = 60) {
  if (!text) return "";
  const first = text.split(/(?<=[.다요])\s/)[0] ?? text;
  return first.length > max ? `${first.slice(0, max - 1)}…` : first;
}
