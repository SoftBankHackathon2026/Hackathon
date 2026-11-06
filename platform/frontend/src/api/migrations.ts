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
  /**
   * 이 구성 요소(워크로드)에 속한 파드. 클러스터 워크로드만 채우고 관리형 서비스는 비운다.
   * 백엔드가 아직 보내지 않으면 undefined: 화면은 detail 의 "준비 x/y" 로 수만 그린다.
   */
  pods?: ComponentPod[];
  /**
   * 브라우저나 클라이언트로 접속할 수 있는 주소(인그레스, 로드 밸런서, 노드 포트).
   * 백엔드가 아직 보내지 않거나 노출된 주소가 없으면 undefined/빈 배열: 화면은 접속 주소를 그리지 않는다.
   */
  endpoints?: ComponentEndpoint[];
};
export type EndpointSource =
  "ingress" | "load_balancer" | "node_port" | "cluster_ip";
export type ComponentEndpoint = {
  /** 열 수 있는 전체 주소(스킴 포함). 예: https://app.example.com, http://192.0.2.10:30002 */
  url: string;
  /** 어디서 나온 주소인가: Ingress/HTTPRoute, Service type=LoadBalancer, NodePort, ClusterIP(클러스터 안에서만) */
  source: EndpointSource;
  /** public: 인터넷에서 접속 가능, private: 사내망/VPN/Tailscale 안에서만 */
  exposure: "public" | "private";
  /** 짧은 용도 이름(선택). 예: 웹 UI, API, 메트릭 */
  label?: string | null;
};
/** Kubernetes Pod 의 status.phase 그대로. */
export type PodPhase =
  "Running" | "Pending" | "Succeeded" | "Failed" | "Unknown";
export type ComponentPod = {
  /** metadata.name */
  name: string;
  phase: PodPhase;
  /** Ready 조건(모든 컨테이너 준비) */
  ready: boolean;
  /** 대기/종료 사유(컨테이너 state.waiting.reason 또는 terminated.reason). 예: CrashLoopBackOff, ImagePullBackOff, OOMKilled */
  reason?: string | null;
  /** 컨테이너 재시작 횟수 합 */
  restarts: number;
  /** spec.nodeName */
  node: string | null;
  /** status.startTime (ISO 8601) */
  started_at: string | null;
  containers: PodContainer[];
};
export type PodContainer = {
  name: string;
  ready: boolean;
  restarts: number;
  /** 이미지 이름:태그(다이제스트는 빼도 됨) */
  image?: string;
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
  /** 클러스터 버전, 노드, 자원 합계. 연결이 끊겼거나 백엔드가 아직 보내지 않으면 null/undefined. */
  cluster?: ClusterInfo | null;
};
/** CPU 는 밀리코어, 메모리는 바이트. */
export type ClusterResources = { cpu_millicores: number; memory_bytes: number };
/** null 은 "읽지 못함"(권한 없음, metrics-server 없음 등). 0 과 다르다. */
export type ClusterInfo = {
  version: string | null;
  node_count: number | null;
  /** Ready 여부를 모르는 노드가 있으면 null */
  ready_nodes: number | null;
  /** 클러스터 합계. 한 노드라도 값을 모르면 null */
  allocatable: ClusterResources | null;
  requests: ClusterResources | null;
  /** metrics-server 실측 */
  usage: ClusterResources | null;
  nodes: ClusterNode[];
};
export type ClusterNode = {
  name: string;
  ready: boolean | null;
  /** cordon 됨(새 파드를 받지 않음) */
  unschedulable: boolean | null;
  roles: string[];
  kubelet_version: string | null;
  os_image: string | null;
  arch: string | null;
  instance_type: string | null;
  zone: string | null;
  allocatable: ClusterResources | null;
  requests: ClusterResources | null;
  usage: ClusterResources | null;
  /** 이 노드에서 도는 파드 수(구성 요소에 속하지 않은 시스템 파드 포함) */
  pods: number | null;
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
