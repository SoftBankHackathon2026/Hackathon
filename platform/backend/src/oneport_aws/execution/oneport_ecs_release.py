from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypedDict

from controlplane.domain.models import DeploymentRequest
from controlplane.providers.contracts import ReleaseKey
from controlplane.repositories.releases import ReleaseStore
from oneport_aws.execution.oneport_ecs_apps import AppRegistry
from oneport_aws.execution.oneport_ecs_runtime import OneportEcsRuntime
from oneport_aws.providers.oneport_ecs.provider import OneportEcsProvider


class ReleaseState(StrEnum):
    QUEUED = "queued"
    INITIALIZING = "initializing"
    INITIALIZED = "initialized"
    MIGRATION_PREPARING = "migration_preparing"
    MIGRATION_SUBMITTING = "migration_submitting"
    MIGRATION_WAITING = "migration_waiting"
    PREPARING = "preparing"
    PREPARED = "prepared"
    SUBMITTING = "submitting"
    WAITING = "waiting"
    CHECKING = "checking"
    RESTORING = "restoring"
    RESTORE_CHECKING = "restore_checking"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


TRAFFIC_STATES = frozenset(
    {
        ReleaseState.WAITING,
        ReleaseState.CHECKING,
        ReleaseState.RESTORING,
        ReleaseState.RESTORE_CHECKING,
    }
)


class PreviousRelease(TypedDict):
    id: str
    request: dict[str, Any] | None


class ReleaseCheckpoint(TypedDict, total=False):
    transport_retries: int
    routing: dict[str, str]
    previous: PreviousRelease | None
    migration_td: str
    migration_task: str
    migration_started_at: float
    prepared: dict[str, Any]
    serving_release: str
    handle: dict[str, Any]
    health_samples: int
    production_attempts: int
    rollback: dict[str, Any]
    restore_samples: int
    restore_attempts: int
    restore_polls: int


@dataclass
class ReleaseContext:
    store: ReleaseStore
    registry: AppRegistry
    runtime: OneportEcsRuntime
    conn: Any
    row: dict[str, Any]
    request: DeploymentRequest
    cp: ReleaseCheckpoint

    @property
    def key(self) -> ReleaseKey:
        return ReleaseKey(value=self.row["id"])

    @property
    def provider(self) -> OneportEcsProvider:
        return self.runtime.provider

    def move(
        self,
        state: ReleaseState,
        message: str,
        *,
        error_code: str | None = None,
        url: str | None = None,
    ) -> None:
        self.cp["transport_retries"] = 0
        self.store.transition(
            self.conn,
            self.row,
            state,
            message,
            checkpoint=self.cp,
            error_code=error_code,
            url=url,
        )

    def poll(self) -> None:
        self.cp["transport_retries"] = 0
        self.store.defer(
            self.conn,
            self.row,
            seconds=self.runtime.settings.poll_seconds,
            checkpoint=self.cp,
        )
