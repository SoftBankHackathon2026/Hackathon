import { request } from "./client";
import type { DataSource } from "./migrations";

export type Severity = "critical" | "high" | "medium" | "low";
export type Category =
  "network" | "workload" | "identity" | "data" | "detection" | "governance";
export type ControlStatus = "pass" | "fail" | "unknown" | "not_applicable";

export type EnvironmentScore = {
  env_id: string;
  name: string;
  score: number;
  passed: number;
  failed: number;
  unknown: number;
  by_severity: Record<Severity, number>;
};
export type ControlResult = {
  status: ControlStatus;
  detail: string;
  resources?: string[];
};
export type Control = {
  id: string;
  title: string;
  category: Category;
  severity: Severity;
  standard_refs: string[];
  remediation: string;
  why?: string;
  scope?: "resource" | "environment";
  results: Record<string, ControlResult>;
};
export type Posture = {
  evaluated_at: string;
  source: DataSource;
  standards: { id: string; name: string }[];
  environments: EnvironmentScore[];
  controls: Control[];
  parity: {
    source_env: string;
    target_env: string;
    regressions: string[];
    improvements: string[];
  };
};

export function getPosture(signal?: AbortSignal) {
  return request<Posture>("security/posture", { signal });
}
