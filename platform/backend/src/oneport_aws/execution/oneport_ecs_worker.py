"""Persistent AWS reconciliation loop. One bounded observation/operation per tick.

Same journal, lock and error policy as execution.kubernetes_worker. Difference
(infra decision, ECS native blue/green): ECS shifts production traffic itself
once green passes the ALB health check, so there is no "verify candidate
before promotion" step. The worker verifies production HTTP during bake time
and asks ECS to roll back if it fails.

queued -> initializing -> initialized [-> migration_preparing -> migration_submitting
-> migration_waiting] -> preparing -> prepared -> submitting -> waiting -> checking -> succeeded

The bracketed release phase runs only when the request has a migration: a one-off
task that must exit 0 before ECS sees the new version (failed otherwise, live
service untouched).
                       \\-> restoring -> restore_checking -> failed (rolled back)
"""

import logging
from collections.abc import Callable

from controlplane.domain.models import DeploymentRequest, Runtime
from controlplane.execution.deletions import Deletion, tick_deletion
from controlplane.execution.image_preflight import prepare_public_image
from controlplane.providers.contracts import (
    Phase,
    ProviderConflict,
    ProviderRejected,
    ProviderUnavailable,
    ReleaseKey,
    SubmissionUncertain,
)
from controlplane.repositories.releases import ReleaseStore
from oneport_aws.candidates import CandidateHandle, PreparedCandidate
from oneport_aws.execution import oneport_ecs_delete
from oneport_aws.execution.oneport_ecs_app_model import ResourceKind
from oneport_aws.execution.oneport_ecs_apps import AppRegistry
from oneport_aws.execution.oneport_ecs_db import provision_step
from oneport_aws.execution.oneport_ecs_initialize import initialize_release
from oneport_aws.execution.oneport_ecs_migrate import run_migration_step
from oneport_aws.execution.oneport_ecs_release import (
    TRAFFIC_STATES,
    ReleaseContext,
    ReleaseState,
)
from oneport_aws.execution.oneport_ecs_rollback import check_rollback, request_rollback

LOG = logging.getLogger(__name__)
MAX_TRANSPORT_RETRIES = 5
MAX_TRANSPORT_BACKOFF_SECONDS = 30


class OneportEcsWorker:
    def __init__(self, store: ReleaseStore, runtime):
        self.store = store
        self.runtime = runtime
        self._handlers: dict[ReleaseState, Callable[[ReleaseContext], None]] = {
            ReleaseState.QUEUED: self._queued,
            ReleaseState.INITIALIZING: initialize_release,
            ReleaseState.INITIALIZED: self._initialized,
            ReleaseState.MIGRATION_PREPARING: run_migration_step,
            ReleaseState.MIGRATION_SUBMITTING: run_migration_step,
            ReleaseState.MIGRATION_WAITING: run_migration_step,
            ReleaseState.PREPARING: self._prepare,
            ReleaseState.PREPARED: self._submit,
            ReleaseState.SUBMITTING: self._submit,
            ReleaseState.WAITING: self._wait,
            ReleaseState.CHECKING: self._check,
            ReleaseState.RESTORING: request_rollback,
            ReleaseState.RESTORE_CHECKING: check_rollback,
        }

    def tick(self) -> None:
        settings = self.runtime.settings
        with self.store.worker_lock() as conn:
            if conn is None:
                return
            if tick_deletion(
                conn, settings.environment_id, self._delete, poll_seconds=settings.poll_seconds
            ):
                return
        self._provision_resources()
        with self.store.worker_lock() as conn:
            if conn is None:
                return
            row = self.store.next(
                conn,
                runtime=Runtime.AWS_ECS,
                application=settings.application,
                environment_id=settings.environment_id,
            )
            if row is None:
                return
            self._run_release_step(conn, row)

    def _run_release_step(self, conn, row) -> None:
        try:
            if not prepare_public_image(self.store, conn, row):
                return
            self._step(conn, row)
        except (ProviderUnavailable, SubmissionUncertain) as exc:
            self._defer_transport_failure(conn, row, exc)
        except Exception as exc:  # noqa: BLE001 -- process boundary, no payload/credential logging
            LOG.error(
                "release=%s state=%s exception=%s", row["id"], row["state"], type(exc).__name__
            )
            self.store.transition(
                conn,
                row,
                ReleaseState.UNCERTAIN,
                "요청·권한·AWS 상태를 대조해야 합니다. 임의 재실행하지 않습니다.",
                error_code=type(exc).__name__,
            )

    def _defer_transport_failure(self, conn, row, exc: Exception) -> None:
        cp = dict(row["checkpoint"])
        count = cp.get("transport_retries", 0) + 1
        cp["transport_retries"] = count
        if count <= MAX_TRANSPORT_RETRIES and not row["expired"]:
            seconds = min(2**count, MAX_TRANSPORT_BACKOFF_SECONDS)
            self.store.defer(conn, row, seconds=seconds, checkpoint=cp)
            return
        self.store.transition(
            conn,
            row,
            ReleaseState.UNCERTAIN,
            "AWS 상태를 확인하지 못했습니다. 운영자 재조회가 필요합니다.",
            checkpoint=cp,
            error_code=type(exc).__name__,
        )

    def _delete(self, deletion: Deletion) -> None:
        self.runtime.connect()
        oneport_ecs_delete.step(deletion, self.runtime.provider, AppRegistry(self.store.dsn))

    def _provision_resources(self) -> None:
        settings = self.runtime.settings
        registry = AppRegistry(self.store.dsn)
        app = registry.get(settings.application, settings.environment_id)
        if app is None:
            return
        pending: list[tuple[ResourceKind, int]] = []
        if app.database_mode == "dedicated" and app.db_state.in_progress:
            pending.append((ResourceKind.DB, settings.db_settle_seconds))
        if app.redis and app.cache_state.in_progress:
            pending.append((ResourceKind.CACHE, settings.cache_settle_seconds))
        for kind, settle in pending:
            try:
                self.runtime.connect()
                result = provision_step(
                    registry, self.runtime.provider, settle_seconds=settle, kind=kind
                )
                if result:
                    LOG.info("%s %s: %s", kind, result.state, result.message)
            except (ProviderUnavailable, SubmissionUncertain) as exc:
                LOG.warning("%s provisioning deferred: %s", kind, type(exc).__name__)

    def _merge_refs(self) -> None:
        settings, provider = self.runtime.settings, self.runtime.provider
        stored = AppRegistry(self.store.dsn).refs(settings.application, settings.environment_id)
        refs = {**settings.secret_refs, **stored}
        if provider.environment.secret_refs != refs:
            provider.environment = provider.environment.model_copy(update={"secret_refs": refs})

    def _register_restore(self, ctx: ReleaseContext) -> str | None:
        restore_of = ctx.registry.restore_of(ctx.key.value)
        if restore_of:
            request = ctx.request
            driver = self.runtime.gateway.registered_driver(
                request.application, request.environment_id
            )
            driver.restores[ctx.key.value] = restore_of
        return restore_of

    def _step(self, conn, row) -> None:
        runtime = self.runtime
        if row["binding_fingerprint"] != runtime.settings.fingerprint():
            raise ProviderConflict("Registered environment changed")
        request = DeploymentRequest.model_validate(row["request"])
        runtime.connect()
        self._merge_refs()
        runtime.settings.validate_request(request, runtime.provider)
        ctx = ReleaseContext(
            store=self.store,
            registry=AppRegistry(self.store.dsn),
            runtime=runtime,
            conn=conn,
            row=row,
            request=request,
            cp=dict(row["checkpoint"]),
        )
        try:
            state = ReleaseState(row["state"])
        except ValueError:
            raise ProviderConflict("Unknown AWS release state") from None
        if self._expire(ctx, state):
            return
        handler = self._handlers.get(state)
        if handler is None:
            raise ProviderConflict("Unknown AWS release state")
        handler(ctx)

    @staticmethod
    def _expire(ctx: ReleaseContext, state: ReleaseState) -> bool:
        if not ctx.row["expired"]:
            return False
        if state not in TRAFFIC_STATES:
            ctx.move(
                ReleaseState.FAILED if state == ReleaseState.QUEUED else ReleaseState.UNCERTAIN,
                "작업 기한이 지났습니다. 생성된 리소스는 조회 후 정리해야 합니다.",
                error_code="deadline_exceeded",
            )
            return True
        if state == ReleaseState.WAITING:
            ctx.move(ReleaseState.RESTORING, "기한 초과. ECS 배포 롤백 의도를 저장했습니다.")
            return True
        return False

    def _queued(self, ctx: ReleaseContext) -> None:
        ctx.move(ReleaseState.INITIALIZING, "앱 runtime role·Target Group·rule·알람을 준비합니다.")
        initialize_release(ctx)

    def _initialized(self, ctx: ReleaseContext) -> None:
        if ctx.request.migration and not ctx.registry.restore_of(ctx.key.value):
            ctx.move(
                ReleaseState.MIGRATION_PREPARING,
                "배포 전 마이그레이션 Task Definition을 등록합니다.",
            )
        else:
            ctx.move(ReleaseState.PREPARING, "Task Definition을 등록합니다.")

    def _prepare(self, ctx: ReleaseContext) -> None:
        restore_of = self._register_restore(ctx)
        try:
            prepared = self.runtime.gateway.prepare(ctx.request, ctx.key)
        except ProviderRejected as exc:
            ctx.move(ReleaseState.FAILED, str(exc), error_code="task_definition_rejected")
            return
        ctx.cp["prepared"] = prepared.model_dump(mode="json")
        task_definition = ctx.cp["prepared"]["checkpoint"]["task_definition_arn"]
        serving = ctx.provider.release_of(task_definition) or ctx.key.value
        ctx.cp["serving_release"] = serving
        ctx.move(ReleaseState.PREPARED, _prepared_message(restore_of, serving, task_definition))

    def _submit(self, ctx: ReleaseContext) -> None:
        if ctx.row["state"] == ReleaseState.PREPARED:
            ctx.move(ReleaseState.SUBMITTING, "ECS 배포 의도를 저장했습니다.")
        self._register_restore(ctx)
        prepared = PreparedCandidate.model_validate(ctx.cp["prepared"])
        ctx.cp["handle"] = self.runtime.gateway.submit(ctx.request, prepared).model_dump(
            mode="json"
        )
        ctx.move(ReleaseState.WAITING, "ECS가 green을 띄우고 트래픽을 전환하길 기다립니다.")

    def _wait(self, ctx: ReleaseContext) -> None:
        handle = CandidateHandle.model_validate(ctx.cp["handle"])
        observed = self.runtime.gateway.observe(ctx.request, handle)
        if observed.phase == Phase.READY:
            ctx.cp["health_samples"], ctx.cp["production_attempts"] = 0, 0
            ctx.move(
                ReleaseState.CHECKING, "ECS가 운영 트래픽을 전환했습니다. 운영 HTTP를 확인합니다."
            )
        elif observed.phase == Phase.FAILED:
            ctx.move(
                ReleaseState.FAILED,
                "ECS가 새 버전을 실패로 판단했습니다. 운영은 이전 버전입니다.",
                error_code=observed.code,
            )
        else:
            ctx.poll()

    def _check(self, ctx: ReleaseContext) -> None:
        cp, settings = ctx.cp, self.runtime.settings
        serving = ReleaseKey(value=cp.get("serving_release", ctx.key.value))
        healthy = self.runtime.probe(ctx.request, serving)
        cp["health_samples"] = cp.get("health_samples", 0) + 1 if healthy else 0
        cp["production_attempts"] = cp.get("production_attempts", 0) + 1
        if cp["health_samples"] >= settings.required_health_samples:
            ctx.move(
                ReleaseState.SUCCEEDED,
                "운영 HTTPS·릴리즈 식별을 연속 확인했습니다.",
                url=ctx.provider.environment.url,
            )
        elif cp["production_attempts"] >= settings.production_check_limit or ctx.row["expired"]:
            ctx.move(ReleaseState.RESTORING, "운영 검증 실패. ECS 롤백 의도를 저장했습니다.")
        else:
            ctx.poll()


def _prepared_message(restore_of: str | None, serving: str, task_definition: str) -> str:
    if restore_of and serving == restore_of:
        revision = task_definition.rsplit("/", 1)[-1]
        return f"이전 Release {restore_of}의 revision {revision}을 그대로 씁니다."
    if restore_of:
        return "이전 revision이 정리돼 같은 이미지·설정으로 다시 등록했습니다."
    return "Task Definition 체크포인트를 저장했습니다."
