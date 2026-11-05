from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import ValidationError

from controlplane.core.errors import (
    InvalidRequest,
    OperationNotFound,
    PlatformError,
    RequestConflict,
    UnsupportedOperation,
)
from controlplane.domain.models import Plan
from controlplane.providers.durable import DurableDeploymentProvider
from controlplane.providers.interface import (
    AppLogProvider,
    DeploymentOperation,
    DeploymentProvider,
    DeploymentSpec,
    DeploymentStatus,
    ReleaseActionProvider,
    ReleaseRestoreProvider,
)
from controlplane.providers.registry import ProviderRegistry
from controlplane.providers.telemetry import TelemetryProvider
from controlplane.repositories.accounts import AccountStore
from controlplane.repositories.deletions import DeletionStore
from controlplane.repositories.releases import ReleaseStore
from controlplane.schemas.accounts import Identity
from controlplane.services.access import TeamAccess
from controlplane.services.plan_errors import plan_error

NO_PROVIDER = "실행 가능한 Provider가 등록되지 않은 환경입니다."
RELEASE_NOT_FOUND = "등록 환경에 속한 Release가 없습니다."
PLAN_CHANGED = "설정이 변경됐습니다. 배포 계획을 다시 확인하세요."
REGISTRATION_REJECTED = "Provider 지원 기능·진행 중인 배포·DB 상태와 등록값을 확인하세요."
APP_DELETED = "이 환경의 앱은 삭제됐습니다. 운영자가 환경을 다시 준비해야 배포할 수 있습니다."


def deletion_info(provider: DeploymentProvider | None) -> dict[str, Any]:
    describe = getattr(provider, "deletion_info", None)
    return describe() if describe else {"supported": False}


def owner_fields(identity: Identity | None) -> dict[str, str]:
    return {"team": identity.team, "created_by": identity.email} if identity else {}


def operation_of(source: Any) -> DeploymentOperation:
    if isinstance(source, dict):
        return DeploymentOperation(**{key: source[key] for key in DeploymentOperation.model_fields})
    return DeploymentOperation(
        **{key: getattr(source, key) for key in DeploymentOperation.model_fields}
    )


@contextmanager
def plan_errors() -> Iterator[None]:
    try:
        yield
    except (RequestConflict, InvalidRequest):
        raise
    except (ValidationError, ValueError, TypeError) as exc:
        raise InvalidRequest(plan_error(exc)) from None


@contextmanager
def release_errors() -> Iterator[None]:
    try:
        yield
    except OperationNotFound:
        raise OperationNotFound(RELEASE_NOT_FOUND) from None
    except PlatformError:
        raise
    except ValueError as exc:
        raise RequestConflict(str(exc)) from None


class DeploymentService:
    def __init__(self, registry: ProviderRegistry, store: ReleaseStore | None) -> None:
        self.registry = registry
        self.store = store
        self.accounts = AccountStore(store) if store else None
        self.deletions = DeletionStore(store.dsn) if store else None
        self.access = TeamAccess(registry, self.accounts)

    def provider(self, environment_id: str) -> DeploymentProvider:
        provider = self.registry.get(environment_id)
        if provider is None:
            raise RequestConflict(NO_PROVIDER)
        return provider

    def plan(self, spec: DeploymentSpec, identity: Identity | None = None) -> Plan:
        self.access.check_plannable(spec.environment_id, identity)
        return self.provider(spec.environment_id).plan(spec)

    def preview(self, spec: DeploymentSpec, identity: Identity | None) -> Plan:
        with plan_errors():
            return self.plan(spec, identity)

    def submit(
        self, spec: DeploymentSpec, operation_key: str, identity: Identity | None = None
    ) -> DeploymentStatus:
        provider = self.provider(spec.environment_id)
        self._require_deployable(provider)
        operation = self.access.as_owner(
            spec.environment_id,
            identity,
            lambda: provider.submit(spec, operation_key, **owner_fields(identity)),
        )
        return provider.observe(operation)

    def submit_planned(
        self,
        spec: DeploymentSpec,
        operation_key: str,
        plan_fingerprint: str | None,
        identity: Identity | None,
    ) -> DeploymentStatus:
        with plan_errors():
            if plan_fingerprint and self.plan(spec, identity).fingerprint != plan_fingerprint:
                raise RequestConflict(PLAN_CHANGED)
            return self.submit(spec, operation_key, identity)

    def observe(self, operation_id: str, identity: Identity | None = None) -> DeploymentStatus:
        row = self.store.get(operation_id) if self.store else None
        if (
            row is None
            or not self.registry.owns(row)
            or (identity and row.get("team") != identity.team)
        ):
            raise OperationNotFound(RELEASE_NOT_FOUND)
        return self._status(row)

    def _status(
        self, row: dict[str, Any], provider: DeploymentProvider | None = None
    ) -> DeploymentStatus:
        provider = provider or self.provider(row["environment_id"])
        if isinstance(provider, DurableDeploymentProvider):
            return provider.snapshot(row)
        return provider.observe(operation_of(row))

    def list_releases(
        self,
        identity: Identity | None = None,
        *,
        environment_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[DeploymentStatus]:
        if self.store is None:
            return []
        rows = self.store.list_with_events(
            team=identity.team if identity else None,
            environment_id=environment_id,
            limit=limit,
            offset=offset,
        )
        adapters: dict[str, DeploymentProvider | None] = {}
        statuses = []
        for row in rows:
            env = row["environment_id"]
            if env not in adapters:
                adapters[env] = self.registry.get(env)
            if provider := self.registry.owner(row, adapters[env]):
                statuses.append(self._status(row, provider))
        return statuses

    def log_provider(self, environment_id: str, identity: Identity | None = None) -> Any:
        self.access.require_owner(environment_id, identity)
        provider = self.provider(environment_id)
        if not isinstance(provider, AppLogProvider):
            raise UnsupportedOperation("이 Provider는 앱 로그 조회를 지원하지 않습니다.")
        return provider

    def telemetry(self, environment_id: str, identity: Identity | None = None) -> TelemetryProvider:
        self.access.require_owner(environment_id, identity)
        provider = self.provider(environment_id)
        if not isinstance(provider, TelemetryProvider):
            raise UnsupportedOperation("이 Provider는 트래픽·trace 조회를 지원하지 않습니다.")
        return provider

    def visible_environments(
        self, items: list[dict[str, Any]], identity: Identity | None
    ) -> list[dict[str, Any]]:
        return self.access.visible(items, identity)

    def register_application(
        self, environment_id: str, identity: Identity | None = None, **settings: Any
    ) -> dict[str, Any]:
        try:
            provider = self.provider(environment_id)
            registered = self.access.as_owner(
                environment_id, identity, lambda: provider.register_application(**settings)
            )
        except ValueError as exc:
            reason = "" if isinstance(exc, ValidationError) else f" ({exc})"
            raise RequestConflict(REGISTRATION_REJECTED + reason) from None
        return self.visible_environments([registered], identity)[0]

    def request_action(
        self, operation_id: str, action: str, identity: Identity | None = None
    ) -> DeploymentStatus:
        with release_errors():
            current = self.observe(operation_id, identity)
            provider = self.provider(current.environment_id)
            if not isinstance(provider, ReleaseActionProvider):
                raise UnsupportedOperation("이 Provider는 수동 전환 API를 지원하지 않습니다.")
            return provider.request_action(operation_of(current), action)

    def restore(
        self, operation_id: str, operation_key: str, identity: Identity | None = None
    ) -> DeploymentStatus:
        with release_errors():
            current = self.observe(operation_id, identity)
            provider = self.provider(current.environment_id)
            if not isinstance(provider, ReleaseRestoreProvider) or not getattr(
                provider, "supports_restore", False
            ):
                raise UnsupportedOperation("이 환경은 이전 버전으로 되돌리기를 지원하지 않습니다.")
            self._require_deployable(provider)
            restored = self.access.as_owner(
                current.environment_id,
                identity,
                lambda: provider.restore(
                    operation_of(current), operation_key, **owner_fields(identity)
                ),
            )
            return provider.observe(restored)

    def _require_deployable(self, provider: DeploymentProvider) -> None:
        if not self.deletions:
            return
        last = self.deletions.latest(provider.environment_id)
        if (
            last
            and last["state"] == "deleted"
            and not deletion_info(provider).get("recreatable", True)
        ):
            raise RequestConflict(APP_DELETED)
