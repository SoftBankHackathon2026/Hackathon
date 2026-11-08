import { request } from "./client";
import type { DataSource } from "./migrations";

export type Severity = "critical" | "high" | "medium" | "low";
export type Category =
  | "network"
  | "workload"
  | "identity"
  | "data"
  | "detection"
  | "governance"
  | "japan";
export type ControlStatus =
  "pass" | "fail" | "unknown" | "not_applicable" | "manual";
/** compliance: 표준 전체 점검, coverage: 고객 측 기술 통제만 점검 */
export type StandardKind = "compliance" | "coverage";
export type Standard = {
  id: string;
  name: string;
  kind?: StandardKind;
  version?: string | null;
  note?: string | null;
};

export type EnvironmentScore = {
  env_id: string;
  name: string;
  score: number;
  passed: number;
  failed: number;
  unknown: number;
  not_applicable?: number;
  manual?: number;
  by_severity: Record<Severity, number>;
};
/** observed: 클러스터·클라우드 API 에서 읽음, declared: fleet.json 신고값 */
export type ResultSource = "observed" | "declared";
export type ControlResult = {
  status: ControlStatus;
  detail: string;
  resources?: string[];
  source?: ResultSource | null;
  /** 실측을 못 해 신고값을 쓰거나 판단하지 못한 이유 */
  fallback_reason?: string | null;
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
  standards: Standard[];
  environments: EnvironmentScore[];
  controls: Control[];
  parity: Parity;
  parities?: Parity[];
};

export type Parity = {
  source_env: string;
  target_env: string;
  regressions: string[];
  improvements: string[];
};

export function getPosture(signal?: AbortSignal) {
  return request<Posture>("security/posture", { signal });
}
