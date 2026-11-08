from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from controlplane.schemas.migrations import DataSource

# not_applicable: 그 환경에 대상이 없음(예: DB 없는 환경의 DB 통제).
# manual: 자동 점검할 수 없어 사람이 증빙으로 확인해야 함. 둘 다 점수 분모에서 빠진다.
ControlStatus = Literal["pass", "fail", "unknown", "not_applicable", "manual"]
Severity = Literal["critical", "high", "medium", "low"]
Category = Literal["network", "workload", "identity", "data", "detection", "governance", "japan"]


class Standard(BaseModel):
    id: str
    name: str
    # compliance: 표준 전체를 점검 / coverage: 고객 측 기술 통제만 점검(조직·인적·물리 통제 제외)
    kind: Literal["compliance", "coverage"] = "compliance"
    version: str | None = None
    note: str | None = None


class SeverityCounts(BaseModel):
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0


class EnvironmentPosture(BaseModel):
    env_id: str
    name: str
    score: int
    passed: int
    failed: int
    unknown: int
    not_applicable: int = 0
    manual: int = 0
    by_severity: SeverityCounts


class ControlResult(BaseModel):
    status: ControlStatus
    detail: str
    resources: list[str] = Field(default_factory=list)


class ControlView(BaseModel):
    id: str
    title: str
    category: Category
    severity: Severity
    standard_refs: list[str] = Field(default_factory=list)
    remediation: str
    why: str = ""
    scope: Literal["resource", "environment"] = "environment"
    results: dict[str, ControlResult]


class Parity(BaseModel):
    source_env: str
    target_env: str
    regressions: list[str]
    improvements: list[str]


class SecurityPosture(BaseModel):
    evaluated_at: datetime
    source: DataSource
    standards: list[Standard]
    environments: list[EnvironmentPosture]
    controls: list[ControlView]
    # 첫 번째 출발→도착 쌍. 이전 화면 호환용으로 남긴다.
    parity: Parity
    # 출발 환경과 각 도착 환경(AWS, GCP 등)의 쌍 전체
    parities: list[Parity] = Field(default_factory=list)
