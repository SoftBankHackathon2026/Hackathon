from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from controlplane.domain.migration import Direction, MigrationStatus, StageProgress

DataSource = Literal["live", "simulated"]


class AppState(BaseModel):
    name: str
    namespace: str
    sync: Literal["Synced", "OutOfSync", "Unknown"]
    health: Literal["Healthy", "Progressing", "Degraded", "Suspended", "Missing", "Unknown"]
    images: list[str] = Field(default_factory=list)
    revision: str | None = None


class DataService(BaseModel):
    kind: Literal["postgres", "kafka", "object_storage", "vector_db"]
    name: str
    managed: bool


ComponentLayer = Literal["entry", "platform", "app", "migration", "data", "link"]
ComponentStatus = Literal["healthy", "degraded", "down", "idle", "unknown"]
PodPhase = Literal["Running", "Pending", "Succeeded", "Failed", "Unknown"]


class ContainerView(BaseModel):
    name: str
    ready: bool
    restarts: int
    image: str


class PodView(BaseModel):
    name: str
    phase: PodPhase
    ready: bool
    reason: str | None = None
    restarts: int
    node: str | None = None
    started_at: datetime | None = None
    containers: list[ContainerView] = Field(default_factory=list)


EndpointSource = Literal["ingress", "load_balancer", "node_port", "cluster_ip"]


class EndpointView(BaseModel):
    url: str
    source: EndpointSource
    exposure: Literal["public", "private"]
    label: str | None = None


class ComponentView(BaseModel):
    id: str
    name: str
    layer: ComponentLayer
    kind: str
    managed: bool
    status: ComponentStatus
    detail: str
    depends_on: list[str] = Field(default_factory=list)
    placement: list[str] = Field(default_factory=list)
    pods: list[PodView] | None = Field(default=None, exclude_if=lambda pods: pods is None)
    endpoints: list[EndpointView] = Field(default_factory=list)


class EnvironmentIssue(BaseModel):
    severity: Literal["critical", "warning"]
    title: str
    detail: str


class EnvironmentView(BaseModel):
    id: str
    name: str
    kind: Literal["k3s", "eks", "gke"]
    provider: Literal["onprem", "aws", "gcp"]
    region: str
    role: Literal["legacy", "target"]
    connected: bool
    observed_at: datetime | None
    apps: list[AppState] = Field(default_factory=list)
    data_services: list[DataService] = Field(default_factory=list)
    components: list[ComponentView] = Field(default_factory=list)
    boundary: list[str] = Field(default_factory=list)
    issues: list[EnvironmentIssue] = Field(default_factory=list)


class WriteCounts(BaseModel):
    committed_source: int = 0
    committed_target: int = 0
    failed: int = 0
    rejected: int = 0
    mismatch: int = 0


class DowntimeEvidence(BaseModel):
    write_pause_seconds: float | None = None
    failed_requests: int = 0
    max_latency_seconds: float | None = None
    p95_latency_seconds: float | None = None


class MigrationSummary(BaseModel):
    job_id: str
    direction: Direction
    source_env: str
    target_env: str
    data_source: str
    data_target: str
    stage: str | None
    status: MigrationStatus
    stage_index: int
    stage_count: int
    updated_at: datetime | None
    writes: WriteCounts
    downtime: DowntimeEvidence
    verify_mismatch: int | None = None


class Fleet(BaseModel):
    generated_at: datetime
    source: DataSource
    environments: list[EnvironmentView]
    migrations: list[MigrationSummary]


class StageView(BaseModel):
    name: str
    title: str
    description: str
    status: StageProgress
    started_at: datetime | None = None
    finished_at: datetime | None = None
    point_of_no_return: bool = False
    evidence: dict[str, Any] = Field(default_factory=dict)


class GateDecision(BaseModel):
    decision_id: str
    stage: Literal["plan", "pre_start", "pre_cutover", "final_verify"]
    outcome: Literal["allow", "deny", "hold", "invalid"]
    reason_codes: list[str] = Field(default_factory=list)
    policy_version: str | None = None
    evaluated_at: datetime


class VerifyCounts(BaseModel):
    B_minus_T: int
    T_minus_B: int
    field_mismatch: int
    S_minus_T: int


class VerifyReport(BaseModel):
    passed: bool
    counts: VerifyCounts
    source_count: int
    target_count: int
    committed_count: int


class VerifyPair(BaseModel):
    pre: VerifyReport | None = None
    post: VerifyReport | None = None


class ConnectorView(BaseModel):
    name: str
    role: Literal["source", "sink"]
    state: str
    tasks_running: int
    tasks_total: int


class LatencyView(BaseModel):
    p50: float | None = None
    p95: float | None = None
    max: float | None = None


class WorkerView(BaseModel):
    state: str
    target: str
    phase: str
    queue_length: int
    rate_per_second: float
    latency: LatencyView
    committed_by_target: dict[str, int]


class MigrationDetail(BaseModel):
    summary: MigrationSummary
    stages: list[StageView]
    gates: list[GateDecision]
    verify: VerifyPair
    connectors: list[ConnectorView]
    worker: WorkerView | None


class SeriesPoint(BaseModel):
    at: datetime
    committed_source: int
    committed_target: int
    write_rate_source: float
    write_rate_target: float
    queue_length: int
    latency_p95: float | None
    latency_max: float | None
    failed_total: int
    worker_state: str
    stage: str | None


class MigrationSeries(BaseModel):
    job_id: str
    step_seconds: int
    points: list[SeriesPoint]


class MigrationEvent(BaseModel):
    at: datetime
    kind: Literal["stage", "gate", "switch", "verify", "warning"]
    title: str
    detail: str


class MigrationIngest(BaseModel):
    cutover: dict[str, Any] | None = None
    policy_decisions: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    verify: dict[str, Any] | None = None
