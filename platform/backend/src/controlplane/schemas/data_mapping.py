from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DataKind = Literal["postgres", "kafka", "object_storage"]
DATA_KINDS: tuple[DataKind, ...] = ("postgres", "kafka", "object_storage")
BindingAction = Literal["map", "create", "exclude"]
BindingBasis = Literal["only_pair", "no_target", "name", "none", "confirmed"]
MappingStatus = Literal["ready", "needs_review", "stale", "unavailable"]


class DataResource(BaseModel):
    id: str
    env_id: str
    kind: DataKind
    name: str
    origin: Literal["managed", "cluster"]
    detail: str | None = None


class DataBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(max_length=300)
    action: BindingAction
    target: str | None = Field(default=None, max_length=300)
    reason: str | None = Field(default=None, max_length=500)


class BindingView(DataBinding):
    kind: DataKind
    basis: BindingBasis
    candidates: list[str] = Field(default_factory=list)


class DataMapping(BaseModel):
    job_id: str
    source_env: str
    target_env: str
    inventory_hash: str
    status: MappingStatus
    required: bool
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    sources: list[DataResource]
    targets: list[DataResource]
    bindings: list[BindingView]
    blockers: list[str] = Field(default_factory=list)


class DataMappingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inventory_hash: str = Field(max_length=100)
    bindings: list[DataBinding] = Field(max_length=200)


class SavedMapping(BaseModel):
    job_id: str
    inventory_hash: str
    bindings: list[DataBinding]
    confirmed_by: str | None = None
    confirmed_at: datetime
