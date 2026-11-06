"""비용 추산의 공통 모델.

환경마다 수집한 자원 목록(CostInventory)을 가격표(PriceSheet)로 계산해 비용 줄(CostLine)을 만든다.
클라우드 청구서를 읽지 않고, 실제로 떠 있는 자원 × 공식 온디맨드 단가로 계산한다.
그래서 같은 방식으로 온프렘·AWS·GCP 를 나란히 놓고, 아직 없는 이전 대상 환경도 추산할 수 있다.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from controlplane.cost.onprem import OnPremReading

HOURS_PER_MONTH = 730

ServiceKind = Literal[
    "virtual_machine",
    "postgres",
    "kafka",
    "object_storage",
    "load_balancer",
    "nat_gateway",
    "kubernetes_control_plane",
]
WorkloadRole = Literal["app", "postgres", "kafka", "object_storage", "self_hosted_data"]
Category = Literal[
    "compute", "control_plane", "storage", "database", "messaging", "network", "onprem"
]
# list_price: 공식 가격표 단가, owner_input: 운영자가 입력한 값(온프렘 장비비·전기료),
# measured: 온프렘 측정기가 읽은 값, not_priced: 단가 없음
Basis = Literal["list_price", "owner_input", "measured", "not_priced"]


class Node(BaseModel):
    name: str
    instance_type: str | None = None
    vcpu: float
    memory_gib: float
    # 이미 다른 워크로드가 요청한 양. 대상 환경의 남는 용량을 계산할 때 쓴다.
    requested_vcpu: float = 0
    requested_memory_gib: float = 0


class Volume(BaseModel):
    owner: str
    gib: float
    storage_class: str | None = None


class WorkloadDemand(BaseModel):
    """한 워크로드가 요청한 자원. 이전할 때 대상 환경에 다시 배치하는 단위다."""

    namespace: str
    name: str
    role: WorkloadRole = "app"
    replicas: int = 1
    vcpu: float = 0
    memory_gib: float = 0
    storage_gib: float = 0


class ManagedService(BaseModel):
    """클러스터 밖 관리형 서비스. 클라우드 태그로 찾는다."""

    kind: ServiceKind
    name: str
    size: str | None = None
    count: int = 1
    storage_gib: float = 0


class CostInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env_id: str
    provider: Literal["onprem", "aws", "gcp"]
    region: str
    observed_at: datetime
    nodes: list[Node] = Field(default_factory=list)
    volumes: list[Volume] = Field(default_factory=list)
    workloads: list[WorkloadDemand] = Field(default_factory=list)
    managed: list[ManagedService] = Field(default_factory=list)
    kubernetes_control_plane: bool = False
    # 어디서 읽었는지. 예: "kubernetes", "aws 태그 Project=oneaction"
    sources: list[str] = Field(default_factory=list)
    # 읽지 못한 것과 이유. 예: "관리형 서비스: AWS 읽기 권한 없음"
    gaps: list[str] = Field(default_factory=list)
    # 온프렘 측정기가 읽은 값(전력 등). 측정기가 없으면 None.
    onprem_reading: OnPremReading | None = None


class CostLine(BaseModel):
    category: Category
    item: str
    quantity: float
    unit: str
    unit_price: float | None
    monthly: float | None
    basis: Basis
    note: str | None = None


class CostBreakdown(BaseModel):
    label: str
    monthly: float
    lines: list[CostLine]
    # 단가가 없어 합계에서 빠진 줄 수
    unpriced: int = 0

    @classmethod
    def of(cls, label: str, lines: list[CostLine]) -> "CostBreakdown":
        return cls(
            label=label,
            monthly=round(sum(line.monthly or 0 for line in lines), 2),
            lines=lines,
            unpriced=sum(1 for line in lines if line.monthly is None),
        )


class PriceRef(BaseModel):
    provider: str
    region: str
    source: str
    fetched_at: datetime


class EnvironmentCost(BaseModel):
    env_id: str
    name: str
    provider: Literal["onprem", "aws", "gcp"]
    currency: Literal["USD"] = "USD"
    observed_at: datetime | None
    # 지금 이 환경에 드는 돈. 온프렘은 운영자가 입력한 장비비·전기료 기준이다.
    current: CostBreakdown | None
    # 온프렘 같은 스펙을 클라우드 가격표로 환산한 값.
    cloud_equivalent: CostBreakdown | None = None
    prices: PriceRef | None = None
    sources: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class CapacityCheck(BaseModel):
    """옮길 워크로드 요청량(여유율 포함)과 대상 노드의 남는 용량."""

    need_vcpu: float
    need_memory_gib: float
    free_vcpu: float
    free_memory_gib: float

    @property
    def fits(self) -> bool:
        return self.need_vcpu <= self.free_vcpu and self.need_memory_gib <= self.free_memory_gib


class MigrationCost(BaseModel):
    job_id: str
    source_env: str
    target_env: str
    currency: Literal["USD"] = "USD"
    source_monthly: float | None
    target_monthly_now: float | None
    # 원본 워크로드를 대상에 옮긴 뒤 대상 환경 월 비용(추산)
    target_monthly_after: float | None
    added: CostBreakdown | None
    # 옮긴 뒤 원본에서 줄어드는 월 비용(옮기는 워크로드 몫)
    source_released: float | None = None
    # 전환을 마친 뒤 월 증감 = 대상 증가분 - 원본 감소분. 음수면 절감.
    monthly_change: float | None = None
    capacity: CapacityCheck | None = None
    capacity_fits: bool | None = None
    # 전환 기간 동안 대상 증가분을 원본과 함께 내는 비용(1회성)
    dual_run_days: int
    dual_run_cost: float | None
    assumptions: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class CostOverview(BaseModel):
    generated_at: datetime
    environments: list[EnvironmentCost]
    migrations: list[MigrationCost]
