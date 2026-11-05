"""Durable, serialized DB/cache steps shared by the worker and operator --wait.

Persist intent before cloud writes. A process death or ambiguous result never
replays a password reset; it requires explicit operator reconciliation.
"""

from dataclasses import dataclass
from types import ModuleType

from psycopg import Connection

from controlplane.providers.contracts import (
    ProviderConflict,
    ProviderRejected,
    ProviderUnavailable,
    SubmissionUncertain,
)
from oneport_aws.execution.oneport_ecs_app_model import (
    RESOURCE_COLUMNS,
    RegisteredApp,
    ResourceKind,
)
from oneport_aws.execution.oneport_ecs_apps import AppRegistry
from oneport_aws.providers.oneport_ecs import cache, database
from oneport_aws.providers.oneport_ecs.provider import OneportEcsProvider
from oneport_aws.providers.oneport_ecs.provisioning import (
    INTERRUPTED_WRITE,
    TERMINAL,
    ResourceState,
    Step,
)


@dataclass(frozen=True)
class ResourceSpec:
    steps: ModuleType
    binding: str
    label: str


RESOURCES = {
    ResourceKind.DB: ResourceSpec(database, database.DB_BINDING, "DB"),
    ResourceKind.CACHE: ResourceSpec(cache, cache.CACHE_BINDING, "캐시"),
}


@dataclass(frozen=True)
class ProvisionResult:
    kind: ResourceKind
    state: ResourceState
    message: str


class WriteIntent:
    def __init__(
        self,
        registry: AppRegistry,
        conn: Connection,
        kind: ResourceKind,
        application: str,
        environment_id: str,
    ):
        self._registry = registry
        self._conn = conn
        self._kind = kind
        self._application = application
        self._environment_id = environment_id
        self.started = False

    def __call__(self, state: ResourceState) -> None:
        self._registry.set_state(
            self._conn, self._kind, self._application, self._environment_id, state
        )
        self._conn.commit()
        self.started = True


def _advance(
    spec: ResourceSpec,
    provider: OneportEcsProvider,
    app: RegisteredApp,
    kind: ResourceKind,
    settle_seconds: float,
    intent: WriteIntent,
) -> Step:
    current = app.state_of(kind)
    if current in INTERRUPTED_WRITE:
        return Step(
            ResourceState.UNCERTAIN,
            f"중단된 {spec.label} 쓰기 작업입니다. 운영자 확인이 필요합니다.",
        )
    try:
        return spec.steps.advance(
            provider,
            current,
            age_seconds=app.age_seconds,
            settle_seconds=settle_seconds,
            before_write=intent,
        )
    except (ProviderRejected, ProviderConflict):
        failed = ResourceState.UNCERTAIN if intent.started else ResourceState.FAILED
        return Step(
            failed,
            f"{spec.label} 준비가 거절되었습니다. 리소스·권한·Secret 삭제 예약 여부를 확인하세요.",
        )
    except (SubmissionUncertain, ProviderUnavailable):
        if not intent.started:
            raise
        return Step(
            ResourceState.UNCERTAIN,
            f"{spec.label} 쓰기 결과를 확인할 수 없습니다. 자동 재시도를 중단합니다.",
        )


def provision_step(
    registry: AppRegistry,
    provider: OneportEcsProvider,
    *,
    settle_seconds: float = 60,
    kind: ResourceKind = ResourceKind.DB,
) -> ProvisionResult | None:
    env = provider.environment
    spec = RESOURCES[kind]
    with registry.locked(env.application, env.environment_id, kind) as (conn, app):
        if app is None:
            return None
        current = app.state_of(kind)
        if not current.in_progress:
            return ProvisionResult(kind, current, f"{RESOURCE_COLUMNS[kind].state}={current}")
        intent = WriteIntent(registry, conn, kind, env.application, env.environment_id)
        step = _advance(spec, provider, app, kind, settle_seconds, intent)
        if step.ref:
            registry.put_ref(conn, env.application, env.environment_id, spec.binding, step.ref)
        if intent.started or step.state != current or step.state in TERMINAL:
            error = step.message if step.state in TERMINAL else None
            registry.set_state(conn, kind, env.application, env.environment_id, step.state, error)
        return ProvisionResult(kind, step.state, step.message)
