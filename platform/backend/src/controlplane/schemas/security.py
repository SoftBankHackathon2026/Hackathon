from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from controlplane.schemas.migrations import DataSource

ControlStatus = Literal["pass", "fail", "unknown", "not_applicable"]
Severity = Literal["critical", "high", "medium", "low"]
Category = Literal["network", "workload", "identity", "data", "detection", "governance"]


class Standard(BaseModel):
    id: str
    name: str


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
    parity: Parity
