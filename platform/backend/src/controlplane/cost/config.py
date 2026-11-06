"""비용 설정. fleet.json 과 따로 둔다(ONEPORT_COST_CONFIG).

- 클라우드 환경: tags 로 이 서비스에 속한 관리형 자원의 범위를 정한다(예: Project=oneaction).
- 온프렘: 장비비·전기료 같은 운영자 입력값과, 같은 스펙을 환산할 클라우드 가격표를 정한다.
- 이전 작업: 옮길 네임스페이스, 데이터 서비스 처리 방식, 여유율, 이중 운영 기간을 정한다.
"""

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_FAMILIES = ["t3", "t4g", "m7g", "m6i"]


class PriceBasis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["aws", "gcp"]
    region: str


class OnPremSpec(BaseModel):
    """온프렘 실제 비용(TCO) 입력값. 비워 두면 해당 줄은 '입력 필요'로 표시하고 합계에서 뺀다."""

    model_config = ConfigDict(extra="forbid")

    hardware_usd: float | None = Field(default=None, ge=0, description="장비 구매가")
    amortization_months: int = Field(default=36, ge=1, description="감가상각 개월 수")
    power_watts: float | None = Field(default=None, ge=0, description="평균 소비 전력")
    electricity_usd_per_kwh: float | None = Field(default=None, ge=0, description="전기 요금 단가")
    other_monthly_usd: float = Field(default=0, ge=0, description="회선·공간 등 기타 월 비용")


class EnvironmentCostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tags: dict[str, str] = Field(default_factory=dict)
    onprem: OnPremSpec = Field(default_factory=OnPremSpec)
    # 온프렘 스펙을 환산할 가격표. 클라우드 환경은 자기 provider·region 가격표를 쓴다.
    equivalent: PriceBasis = Field(
        default_factory=lambda: PriceBasis(provider="aws", region="ap-northeast-2")
    )


class MigrationCostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 옮길 네임스페이스. 비우면 원본 환경의 app_namespace 하나.
    namespaces: list[str] = Field(default_factory=list)
    # managed: Postgres·Kafka·오브젝트 스토리지를 대상 클라우드 관리형으로 바꾼다. self_hosted: 그대로 띄운다.
    data_strategy: Literal["managed", "self_hosted"] = "managed"
    headroom: float = Field(default=0.3, ge=0, le=3)
    min_nodes: int = Field(default=2, ge=1)
    instance_families: list[str] = Field(default_factory=lambda: list(DEFAULT_FAMILIES))
    dual_run_days: int = Field(default=14, ge=0, le=365)


class CostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environments: dict[str, EnvironmentCostConfig] = Field(default_factory=dict)
    migrations: dict[str, MigrationCostConfig] = Field(default_factory=dict)

    def environment(self, env_id: str) -> EnvironmentCostConfig:
        return self.environments.get(env_id) or EnvironmentCostConfig()

    def migration(self, job_id: str) -> MigrationCostConfig:
        return self.migrations.get(job_id) or MigrationCostConfig()


def load_cost_config(path: str | None = None) -> CostConfig:
    path = path or os.environ.get("ONEPORT_COST_CONFIG")
    if not path:
        return CostConfig()
    return CostConfig.model_validate_json(Path(path).read_text())
