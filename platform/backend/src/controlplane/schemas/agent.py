import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

AGENT_ID_PATTERN = r"^[A-Za-z0-9_.-]{1,64}$"
RUN_ID_PATTERN = r"^run-[0-9a-f]{6,32}$"
DETAIL_LIMIT_BYTES = 64 * 1024
SECRET_KEY_MARKERS = ("password", "secret", "token", "dsn")
CONFIG_LIMIT_BYTES = 16 * 1024
CONFIG_KEY_MARKERS = (*SECRET_KEY_MARKERS, "webhook")
THREAD_TS_PATTERN = r"^(|\d{6,}\.\d{1,9})$"

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
RERUN_STATES: frozenset[str] = frozenset(
    {
        "HANDED_OFF",
        "PROBE_FAILED",
        "REJECTED",
        "UNRESOLVED",
        "ERROR",
        "CAS_CONFLICT",
        "RESTART_FAILED",
    }
)
CommandKind = Literal["settings", "rerun"]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


ReportedTime = Annotated[datetime, AfterValidator(_aware)]


def secret_keys(value: Any, markers: tuple[str, ...] = SECRET_KEY_MARKERS) -> list[str]:
    if isinstance(value, dict):
        found = [key for key in value if any(marker in str(key).lower() for marker in markers)]
        return found + [key for item in value.values() for key in secret_keys(item, markers)]
    if isinstance(value, list):
        return [key for item in value for key in secret_keys(item, markers)]
    return []


class AgentHeartbeat(BaseModel):
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    version: str = Field(max_length=64)
    auto_apply: bool
    interval_seconds: int = Field(ge=10, le=3600)
    observed_at: ReportedTime
    config: dict[str, Any] | None = None

    @field_validator("config")
    @classmethod
    def safe_config(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        if len(json.dumps(value, ensure_ascii=False).encode()) > CONFIG_LIMIT_BYTES:
            raise ValueError("config too large")
        if secret_keys(value, CONFIG_KEY_MARKERS):
            raise ValueError("config must not carry credentials")
        return value


class AgentCommand(BaseModel):
    id: int
    kind: CommandKind
    payload: dict[str, Any]
    requested_by: str
    requested_at: datetime


class AgentStatus(BaseModel):
    agent_id: str
    version: str
    auto_apply: bool
    interval_seconds: int
    observed_at: datetime
    received_at: datetime
    online: bool
    config: dict[str, Any] | None
    pending_commands: list[AgentCommand]


class AgentStatusList(BaseModel):
    agents: list[AgentStatus]
    can_command: bool


class SettingsChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_apply: StrictBool | None = None
    interval_seconds: StrictInt | None = Field(default=None, ge=30, le=3600)
    slack_enabled: StrictBool | None = None
    slack_thread_ts: str | None = Field(default=None, pattern=THREAD_TS_PATTERN)

    @model_validator(mode="before")
    @classmethod
    def present_values(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not value:
            raise ValueError("settings change must not be empty")
        if any(item is None for item in value.values()):
            raise ValueError("settings values must not be null")
        return value


class CommandAccepted(BaseModel):
    id: int
    kind: CommandKind


class CommandAck(BaseModel):
    result: str = Field(max_length=200)


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
    can_rerun: bool


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
