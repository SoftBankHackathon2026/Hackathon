from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from controlplane.domain.migration import Direction, Stage
from controlplane.schemas.migrations import (
    AppState,
    ConnectorView,
    DataService,
    GateDecision,
    VerifyReport,
    WorkerView,
)


class DeclaredFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threat_detection: bool | None = None
    posture_service: bool | None = None
    audit_log: bool | None = None
    flow_logs: bool | None = None
    db_tls_required: bool | None = None
    db_public: bool | None = None
    db_encrypted_at_rest: bool | None = None
    db_backups: bool | None = None
    workload_identity: bool | None = None
    ingress_tls: bool | None = None
    waf: bool | None = None
    policy_gates: bool | None = None


class ComponentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    name: str
    layer: Literal["entry", "platform", "app", "migration", "data", "link"]
    kind: str
    managed: bool = False
    workload: str | None = Field(
        default=None, pattern=r"^[a-z0-9-]+/(deployment|statefulset)/[a-z0-9-]+$"
    )
    argo_app: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    note: str | None = None
    placement: list[str] = Field(default_factory=list)
    endpoint_hints: list[str] = Field(default_factory=list)

    @field_validator("endpoint_hints")
    @classmethod
    def lowercase_hints(cls, hints: list[str]) -> list[str]:
        return [hint.lower() for hint in hints]


class EnvironmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    name: str
    kind: Literal["k3s", "eks", "gke"]
    provider: Literal["onprem", "aws", "gcp"]
    region: str
    role: Literal["legacy", "target"]
    kubeconfig: str | None = None
    context: str | None = None
    # 플랫폼이 이 클러스터 안에서 실행될 때 kubeconfig 대신 Pod의 ServiceAccount로 읽는다.
    in_cluster: bool = False
    app_namespace: str
    argocd_namespace: str = "argocd"
    cluster_path: list[str] = Field(default_factory=list)
    data_services: list[DataService] = Field(default_factory=list)
    components: list[ComponentConfig] = Field(default_factory=list)
    boundary: list[str] = Field(default_factory=list)
    declared: DeclaredFacts = Field(default_factory=DeclaredFacts)


class MigrationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    direction: Direction
    source_env: str
    target_env: str
    data_source: str
    data_target: str
    worker_url: str | None = None
    connect_url: str | None = None


class FleetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["live", "simulated"] = "simulated"
    environments: list[EnvironmentConfig]
    migrations: list[MigrationConfig]
    sample_seconds: int = Field(default=5, ge=1, le=60)

    def environment(self, env_id: str) -> EnvironmentConfig:
        for environment in self.environments:
            if environment.id == env_id:
                return environment
        raise KeyError(env_id)

    def migration(self, job_id: str) -> MigrationConfig | None:
        return next((m for m in self.migrations if m.job_id == job_id), None)


@dataclass(frozen=True)
class WorkerSample:
    at: datetime
    state: str
    target: str
    phase: str
    queue_length: int
    committed_by_target: dict[str, int]
    counters: dict[str, int]
    latency_p95: float | None
    latency_max: float | None
    rate_per_second: float = 1.0
    latency_p50: float | None = None


@dataclass(frozen=True)
class CutoverRecord:
    stage: Stage
    checkpoint: dict[str, Any]
    updated_at: datetime
    stage_times: list[tuple[Stage, datetime]] = field(default_factory=list)


@dataclass(frozen=True)
class MigrationState:
    config: MigrationConfig
    cutover: CutoverRecord | None
    gates: list[GateDecision]
    verify_pre: VerifyReport | None
    verify_post: VerifyReport | None
    connectors: list[ConnectorView]
    worker: WorkerView | None


@dataclass(frozen=True)
class Workload:
    namespace: str
    name: str
    images: list[str]
    run_as_non_root: bool
    read_only_root: bool
    drops_all_capabilities: bool
    privileged: bool


@dataclass(frozen=True)
class InventoryItem:
    namespace: str
    kind: str
    name: str
    ready: int
    desired: int
    images: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    uses: list[str] = field(default_factory=list)
    uses_external: list[str] = field(default_factory=list)
    ingress_classes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.name}"


@dataclass(frozen=True)
class ClusterFacts:
    observed_at: datetime | None
    connected: bool
    namespace_labels: dict[str, dict[str, str]] = field(default_factory=dict)
    default_deny_namespaces: set[str] = field(default_factory=set)
    network_policy_count: dict[str, int] = field(default_factory=dict)
    workloads: list[Workload] = field(default_factory=list)
    wildcard_bindings: list[str] | None = None
    argocd_admin_enabled: bool | None = None
    argocd_anonymous_enabled: bool | None = None
    external_secrets: int | None = None
    resource_quota_namespaces: set[str] = field(default_factory=set)
    apps: list[AppState] = field(default_factory=list)
    workload_ready: dict[str, tuple[int, int]] = field(default_factory=dict)
    inventory: list[InventoryItem] | None = None
    read_status: int | None = None


@dataclass(frozen=True)
class EnvironmentSnapshot:
    config: EnvironmentConfig
    cluster: ClusterFacts
