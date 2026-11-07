import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator

AGENT_ID_PATTERN = r"^[A-Za-z0-9_.-]{1,64}$"
RUN_ID_PATTERN = r"^run-[0-9a-f]{6,32}$"
DETAIL_LIMIT_BYTES = 64 * 1024
SECRET_KEY_MARKERS = ("password", "secret", "token", "dsn")

RunState = Literal[
    "NOTHING_TO_FIX",
    "CHECKING",
    "WAITING_APPROVAL",
    "APPLYING",
    "RESOLVED",
    "UNRESOLVED",
    "HANDED_OFF",
    "REJECTED",
    "PROBE_FAILED",
    "CAS_CONFLICT",
    "RESTART_FAILED",
    "ERROR",
]
Decision = Literal["approve", "reject"]
WAITING_APPROVAL: RunState = "WAITING_APPROVAL"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


ReportedTime = Annotated[datetime, AfterValidator(_aware)]


def secret_keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        found = [
            key for key in value if any(marker in str(key).lower() for marker in SECRET_KEY_MARKERS)
        ]
        return found + [key for item in value.values() for key in secret_keys(item)]
    if isinstance(value, list):
        return [key for item in value for key in secret_keys(item)]
    return []


class AgentHeartbeat(BaseModel):
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    version: str = Field(max_length=64)
    auto_apply: bool
    interval_seconds: int = Field(ge=10, le=3600)
    observed_at: ReportedTime


class AgentStatus(BaseModel):
    agent_id: str
    version: str
    auto_apply: bool
    interval_seconds: int
    observed_at: datetime
    received_at: datetime
    online: bool


class AgentStatusList(BaseModel):
    agents: list[AgentStatus]


class RunEvent(BaseModel):
    at: str = Field(max_length=64)
    state: str = Field(max_length=64)
    message: str = Field(max_length=2000)


class RunReport(BaseModel):
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    job_id: str = Field(min_length=1, max_length=128)
    state: RunState
    created_at: ReportedTime
    updated_at: ReportedTime
    detail: dict[str, Any] = Field(default_factory=dict)
    events: list[RunEvent] = Field(default_factory=list, max_length=200)

    @field_validator("detail")
    @classmethod
    def safe_detail(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, ensure_ascii=False).encode()) > DETAIL_LIMIT_BYTES:
            raise ValueError("detail too large")
        if secret_keys(value):
            raise ValueError("detail must not carry credentials")
        return value


class StoredRun(BaseModel):
    run_id: str
    agent_id: str
    job_id: str
    state: RunState
    created_at: datetime
    updated_at: datetime
    detail: dict[str, Any]
    events: list[RunEvent]


class ChangeSummary(BaseModel):
    key: str
    value: str


class RunSummary(BaseModel):
    reason_codes: list[str]
    changes: list[ChangeSummary]
    diagnosis: str | None


class RunListItem(BaseModel):
    run_id: str
    agent_id: str
    job_id: str
    state: RunState
    created_at: datetime
    updated_at: datetime
    summary: RunSummary


class RunList(BaseModel):
    runs: list[RunListItem]


class PendingApproval(BaseModel):
    id: int
    decision: Decision
    requested_by: str
    requested_at: datetime


class RunDetail(RunListItem):
    detail: dict[str, Any]
    events: list[RunEvent]
    pending_approval: PendingApproval | None
    can_decide: bool


class ApprovalTicket(BaseModel):
    id: int
    run_id: str
    decision: Decision
    requested_by: str
    requested_at: datetime


class DecisionAccepted(BaseModel):
    id: int
    decision: Decision


class ApprovalAck(BaseModel):
    result_state: str = Field(max_length=32)
