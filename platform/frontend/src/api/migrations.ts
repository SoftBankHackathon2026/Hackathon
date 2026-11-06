import { request } from "./client";

export type DataSource = "live" | "simulated";
export type SyncState = "Synced" | "OutOfSync" | "Unknown";
export type HealthState =
  "Healthy" | "Progressing" | "Degraded" | "Suspended" | "Missing" | "Unknown";
export type MigrationStatus =
  | "pending"
  | "running"
  | "waiting_approval"
  | "completed"
  | "aborted"
  | "needs_recovery"
  | "rolled_back";
export type StageStatus = "done" | "current" | "pending" | "failed" | "skipped";
export type GateStage = "plan" | "pre_start" | "pre_cutover" | "final_verify";
export type GateOutcome = "allow" | "deny" | "hold" | "invalid";

export type FleetApp = {
  name: string;
  namespace: string;
  sync: SyncState;
  health: HealthState;
  images: string[];
  revision: string | null;
};
export type DataService = {
  kind: "postgres" | "kafka" | "object_storage" | "vector_db";
  name: string;
  managed: boolean;
};
export type ComponentLayer =
  "entry" | "platform" | "app" | "migration" | "data" | "link";
export type ComponentStatus =
  "healthy" | "degraded" | "down" | "idle" | "unknown";
export type EnvComponent = {
  id: string;
  name: string;
  layer: ComponentLayer;
  kind: string;
  managed: boolean;
  status: ComponentStatus;
  detail: string;
  depends_on: string[];
  placement?: string[];
};
export type EnvIssue = {
  severity: "critical" | "warning";
  title: string;
  detail: string;
};
export type FleetEnvironment = {
  id: string;
  name: string;
  kind: "k3s" | "eks" | "gke";
  provider: "onprem" | "aws" | "gcp";
  region: string;
  role: "legacy" | "target";
  connected: boolean;
  observed_at: string | null;
  apps: FleetApp[];
  data_services: DataService[];
  components: EnvComponent[];
  issues: EnvIssue[];
  boundary?: string[];
};
export type MigrationSummary = {
  job_id: string;
  direction: "forward" | "return";
  source_env: string;
  target_env: string;
  data_source: string;
  data_target: string;
  stage: string | null;
  status: MigrationStatus;
  stage_index: number;
  stage_count: number;
  updated_at: string | null;
  writes: {
    committed_source: number;
    committed_target: number;
    failed: number;
    rejected: number;
    mismatch: number;
  };
  downtime: {
    write_pause_seconds: number | null;
    failed_requests: number;
    max_latency_seconds: number | null;
    p95_latency_seconds: number | null;
  };
  verify_mismatch: number | null;
};
export type Fleet = {
  generated_at: string;
  source: DataSource;
  environments: FleetEnvironment[];
  migrations: MigrationSummary[];
};

export type StageInfo = {
  name: string;
  title: string;
  description: string;
  status: StageStatus;
  started_at: string | null;
  finished_at: string | null;
  point_of_no_return: boolean;
  evidence: Record<string, unknown>;
};
export type Gate = {
  decision_id: string;
  stage: GateStage;
  outcome: GateOutcome;
  reason_codes: string[];
  policy_version: string | null;
  evaluated_at: string;
};
export type VerifyCounts = {
  B_minus_T: number;
  T_minus_B: number;
  field_mismatch: number;
  S_minus_T: number;
};
export type VerifyReport = {
  passed: boolean;
  counts: VerifyCounts;
  source_count: number;
  target_count: number;
  committed_count: number;
};
export type Connector = {
  name: string;
  role: "source" | "sink";
  state: string;
  tasks_running: number;
  tasks_total: number;
};
export type Worker = {
  state: string;
  target: string;
  phase: string;
  queue_length: number;
  rate_per_second: number;
  latency: { p50: number | null; p95: number | null; max: number | null };
  committed_by_target: Record<string, number>;
};
export type MigrationDetail = {
  summary: MigrationSummary;
  stages: StageInfo[];
  gates: Gate[];
  verify: { pre: VerifyReport | null; post: VerifyReport | null };
  connectors: Connector[];
  worker: Worker | null;
};
export type SeriesPoint = {
  at: string;
  committed_source: number;
  committed_target: number;
  write_rate_source: number;
  write_rate_target: number;
  queue_length: number;
  latency_p95: number | null;
  latency_max: number | null;
  failed_total: number;
  worker_state: string;
  stage: string | null;
};
export type MigrationSeries = {
  job_id: string;
  step_seconds: number;
  points: SeriesPoint[];
};
export type MigrationEvent = {
  at: string;
  kind: "stage" | "gate" | "switch" | "verify" | "warning";
  title: string;
  detail: string;
};

export function getFleet(signal?: AbortSignal) {
  return request<Fleet>("fleet", { signal });
}

export function getMigration(jobId: string, signal?: AbortSignal) {
  return request<MigrationDetail>(`migrations/${encodeURIComponent(jobId)}`, {
    signal,
  });
}

export function getSeries(
  jobId: string,
  minutes: number,
  signal?: AbortSignal,
) {
  return request<MigrationSeries>(
    `migrations/${encodeURIComponent(jobId)}/series?minutes=${minutes}`,
    { signal },
  );
}

export function getEvents(jobId: string, signal?: AbortSignal) {
  return request<MigrationEvent[]>(
    `migrations/${encodeURIComponent(jobId)}/events`,
    { signal },
  );
}
