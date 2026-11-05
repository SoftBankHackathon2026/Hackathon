"""Shared durable submission/observation; concrete providers own translation and execution."""

import hashlib
import json
import re
from abc import ABC, abstractmethod

from controlplane.core.errors import OperationNotFound, RequestConflict, UnsupportedOperation
from controlplane.domain.models import Model, Plan, Step
from controlplane.providers.interface import DeploymentOperation, DeploymentSpec, DeploymentStatus
from controlplane.repositories import restores
from controlplane.repositories.releases import ReleaseStore


class DurableDeploymentProvider(ABC):
    service_access = "public"
    # Earlier succeeded releases can be deployed again from the console.
    supports_restore = False

    def __init__(self, settings, *, store=None):
        self.settings = settings
        self.store = store or ReleaseStore(settings.database_url.get_secret_value())
        if self.store.dsn != settings.database_url.get_secret_value():
            raise ValueError("Provider and journal must use the same database")

    def initialize(self):
        """Optional provider-local journal tables; no cloud side effects."""

    @abstractmethod
    def parse_request(self, payload: dict) -> Model:
        """Private translation into the existing provider worker's request contract."""

    @abstractmethod
    def fingerprint(self) -> str: ...

    @abstractmethod
    def plan_steps(self, request: Model) -> list[Step]: ...

    def _translate(self, spec):
        # Revalidate even SDK callers who used model_copy/model_construct.
        spec = DeploymentSpec.model_validate(spec.requested())
        if (spec.application, spec.environment_id) != (self.application, self.environment_id):
            raise ValueError("Spec does not belong to this provider binding")
        capabilities = self.capabilities()
        if spec.source.kind not in capabilities.source_kinds:
            raise UnsupportedOperation("이 Provider에서 지원하지 않는 앱 입력 방식입니다.")
        payload = spec.requested()
        for optional in ("migration", "rollout", "instrumentation", "tracing_enabled"):
            if payload.get(optional) is None:
                payload.pop(optional, None)
        requested_fields = set(payload) - {"application", "environment_id", "source"}
        if requested_fields - set(capabilities.configurable_fields):
            raise UnsupportedOperation("이 Provider에서 변경을 지원하지 않는 설정입니다.")
        resources = spec.resources
        if (
            resources
            and resources.database != "none"
            and resources.database_mode not in capabilities.database_modes
        ):
            raise UnsupportedOperation("이 Provider에서 지원하지 않는 DB 방식입니다.")
        return self.parse_request(payload)

    def validate(self, spec: DeploymentSpec) -> None:
        self._translate(spec)

    def plan(self, spec: DeploymentSpec) -> Plan:
        request = self._translate(spec)
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "binding": self.fingerprint(),
                    "request": request.model_dump(mode="json"),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return Plan(
            environment_id=self.environment_id,
            runtime=self.runtime,
            application=self.application,
            fingerprint=fingerprint,
            blockers=[],
            steps=self.plan_steps(request),
            notes=["계획 확인은 작업을 실행하지 않습니다. ‘배포하기’를 눌러 실행하세요."],
        )

    def submit(
        self,
        spec: DeploymentSpec,
        operation_key: str,
        *,
        team: str | None = None,
        created_by: str | None = None,
    ) -> DeploymentOperation:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", operation_key):
            raise ValueError("Invalid operation key")
        request = self._translate(spec)
        row = self.store.enqueue(
            request,
            operation_key,
            self.fingerprint(),
            runtime=self.runtime,
            timeout_seconds=getattr(self.settings, "release_timeout_seconds", 720),
            team=team,
            created_by=created_by,
        )
        return DeploymentOperation(**{key: row[key] for key in DeploymentOperation.model_fields})

    def observe(self, operation: DeploymentOperation) -> DeploymentStatus:
        operation = DeploymentOperation.model_validate(operation.model_dump())
        if (operation.runtime, operation.application, operation.environment_id) != (
            self.runtime,
            self.application,
            self.environment_id,
        ):
            raise OperationNotFound("Operation does not belong to this provider binding")
        row = self.store.get(operation.id)
        if row is None or (row["runtime"], row["application"], row["environment_id"]) != (
            self.runtime,
            self.application,
            self.environment_id,
        ):
            raise OperationNotFound("Operation not found in this provider binding")
        return self.snapshot(row)

    def snapshot(self, row) -> DeploymentStatus:
        """Read a persisted observation, never a cloud SDK readiness shortcut."""
        phase = row["state"]
        source = row["request"]["source"]
        return DeploymentStatus(
            **{key: row[key] for key in DeploymentOperation.model_fields},
            status=phase if phase in {"queued", "succeeded", "failed", "uncertain"} else "running",
            phase=phase,
            image=source.get("image") or row["checkpoint"].get("image"),
            repository=source.get("repository"),
            service_url=row["service_url"],
            service_access=self.service_access,
            error_code=row["error_code"],
            team=row.get("team"),
            created_by=row.get("created_by"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            events=[
                {"phase": e["state"], "message": e["message"], "created_at": e["created_at"]}
                for e in row.get("events", [])
            ],
            restore_of=restores.restore_of(self.store, row["id"]),
        )

    def restore(
        self,
        operation: DeploymentOperation,
        operation_key: str,
        *,
        team: str | None = None,
        created_by: str | None = None,
    ) -> DeploymentOperation:
        """Roll back to an earlier version: a new release with that release's request.

        It runs through the provider's normal workflow (traffic shift, checks, automatic
        rollback). Its migration is not run again: the schema already moved forward and
        an old migration command may not apply to it.
        """
        if not self.supports_restore:
            raise UnsupportedOperation("이 환경은 이전 버전으로 되돌리기를 지원하지 않습니다.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", operation_key):
            raise ValueError("Invalid operation key")
        old = self.store.get(operation.id)
        if old is None or (old["runtime"], old["application"], old["environment_id"]) != (
            self.runtime,
            self.application,
            self.environment_id,
        ):
            raise OperationNotFound("등록 환경에 속한 Release가 없습니다.")
        if old["state"] != "succeeded":
            raise RequestConflict("성공했던 배포로만 되돌릴 수 있습니다.")
        payload = dict(old["request"])
        if payload.get("migration") is not None:
            payload["migration"] = None
        try:
            request = self.parse_request(payload)
        except RequestConflict:
            raise
        except ValueError:
            raise RequestConflict(
                "그 배포의 이미지나 설정을 지금 환경에서 다시 쓸 수 없어 되돌릴 수 없습니다. "
                "(허용 이미지·포트·DB 설정·배포 방식이 바뀌었는지 확인하세요)"
            ) from None
        row = self.store.enqueue(
            request,
            operation_key,
            self.fingerprint(),
            runtime=self.runtime,
            timeout_seconds=getattr(self.settings, "release_timeout_seconds", 720),
            team=team,
            created_by=created_by,
        )
        restores.record(self.store, row["id"], old["id"])
        return DeploymentOperation(**{key: row[key] for key in DeploymentOperation.model_fields})

    def register_application(self, **settings):
        raise UnsupportedOperation("이 Provider는 앱 등록 API를 지원하지 않습니다.")

    def deletion_info(self) -> dict:
        """What deleting this app removes. Providers with a deletion step override this.

        removes / data / keeps: plain labels for the confirmation dialog.
        recreatable: whether a later deploy rebuilds the app in this environment.
        """
        return {"supported": False, "reason": "이 환경은 앱 삭제를 지원하지 않습니다."}
