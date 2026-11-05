from collections.abc import Callable
from dataclasses import dataclass

from controlplane.execution.gcp.context import StepContext
from controlplane.execution.gcp.errors import (
    CacheSubmissionUnknown,
    DataProvisionError,
    MigrationError,
)
from controlplane.execution.gcp.states import ReleaseState
from controlplane.providers.contracts import ProviderConflict

PREPARING_OR_QUEUED = frozenset({ReleaseState.QUEUED, ReleaseState.DATA_PREPARING})
REDIS_OR_QUEUED = frozenset({ReleaseState.QUEUED, ReleaseState.REDIS_PREPARING})

DATA_PHASE_MESSAGES = {
    "instance": "Cloud SQL 인스턴스 생성 중",
    "database": "앱 데이터베이스 준비 중",
    "secret": "Secret Manager 준비 중",
    "credentials": "앱 DB 계정과 연결 정보 준비 중",
}
REDIS_PHASE_MESSAGES = {
    "instance": "Memorystore Redis 생성 작업을 기다립니다.",
    "connection": "Redis AUTH·TLS 연결 Secret을 준비합니다.",
}
MIGRATION_PHASE_MESSAGES = {
    "job": "Migration Job을 후보 이미지와 명령으로 준비합니다.",
    "waiting": "Migration Job 실행을 기다립니다.",
}


@dataclass(frozen=True)
class ProvisionStage:
    applies: bool
    preparing: ReleaseState
    binding_key: str
    phase_key: str
    validate: Callable[[], None]
    step: Callable[[object], tuple[str, dict | None]]
    start_message: str
    ready_message: str
    phase_message: Callable[[str], str]
    conflict_message: str
    failure_suffix: str
    error_code: str


def run_provision_stage(ctx: StepContext, stage: ProvisionStage) -> None:
    cp = ctx.cp
    try:
        stage.validate()
        if ctx.state == ReleaseState.QUEUED:
            ctx.move(stage.preparing, stage.start_message)
            return
        phase, binding = stage.step(ctx.conn)
        if binding:
            cp[stage.binding_key] = binding
            ctx.move(ReleaseState.QUEUED, stage.ready_message)
        elif cp.get(stage.phase_key) != phase:
            cp[stage.phase_key] = phase
            ctx.move(stage.preparing, stage.phase_message(phase))
        else:
            ctx.poll()
    except CacheSubmissionUnknown as exc:
        ctx.move(ReleaseState.UNCERTAIN, str(exc), error_code="gcp_cache_submission_unknown")
    except (DataProvisionError, ProviderConflict) as exc:
        message = str(exc) if isinstance(exc, DataProvisionError) else stage.conflict_message
        ctx.move(
            ReleaseState.FAILED,
            message + stage.failure_suffix,
            error_code=stage.error_code,
        )


def _read_registered_service(ctx: StepContext) -> None:
    if ctx.initial:
        ctx.runtime.app.read(ctx.conn)
    else:
        ctx.provider.read()


def data_stage(ctx: StepContext) -> ProvisionStage:
    rt = ctx.runtime
    return ProvisionStage(
        applies=(
            ctx.state in PREPARING_OR_QUEUED
            and ctx.request.resources.database == "postgresql"
            and not ctx.provider.data_binding
        ),
        preparing=ReleaseState.DATA_PREPARING,
        binding_key="data_binding",
        phase_key="data_phase",
        validate=lambda: _read_registered_service(ctx),
        step=lambda conn: rt.data.step(conn),
        start_message="앱 전용 Cloud SQL·Secret 준비를 시작합니다. 기존 트래픽은 유지합니다.",
        ready_message="DB 준비 완료. DATABASE_URL과 사설 연결을 후보 버전에 적용합니다.",
        phase_message=DATA_PHASE_MESSAGES.__getitem__,
        conflict_message="등록된 Cloud Run 서비스가 변경됐습니다.",
        failure_suffix=" 기존 앱 트래픽과 생성한 데이터는 보존했습니다.",
        error_code="gcp_data_preparation_failed",
    )


def cache_stage(ctx: StepContext) -> ProvisionStage:
    rt, provider = ctx.runtime, ctx.provider

    def validate() -> None:
        if ctx.initial:
            template = rt.app.empty_service().template
            provider.apply_binding(template, rt.cache.binding(), "CACHE_CONFIG")
            provider.apply_binding(template, rt.redis.binding(), "REDIS_URL")
        else:
            provider.validate_cache_target(rt.cache.binding())
            provider.validate_redis_target(rt.redis.binding())

    return ProvisionStage(
        applies=(
            ctx.state in PREPARING_OR_QUEUED
            and ctx.request.resources.redis
            and not provider.cache_binding
        ),
        preparing=ReleaseState.DATA_PREPARING,
        binding_key="cache_binding",
        phase_key="cache_phase",
        validate=validate,
        step=lambda conn: rt.cache.step(conn),
        start_message="앱 전용 Redis 캐시 준비를 시작합니다.",
        ready_message="캐시 준비 완료. CACHE_CONFIG를 후보 버전에 연결합니다.",
        phase_message=lambda _phase: "Redis 캐시 생성·연결 정보를 준비 중입니다.",
        conflict_message="등록된 Cloud Run 캐시 연결 설정이 다릅니다.",
        failure_suffix=" 기존 앱과 캐시는 보존했습니다.",
        error_code="gcp_cache_preparation_failed",
    )


def redis_stage(ctx: StepContext) -> ProvisionStage:
    rt = ctx.runtime

    def validate() -> None:
        _read_registered_service(ctx)
        if rt.redis is None:
            raise DataProvisionError("이 환경에는 Redis 생성 설정이 없습니다.")

    return ProvisionStage(
        applies=(
            ctx.state in REDIS_OR_QUEUED
            and ctx.request.resources.redis
            and not ctx.provider.redis_binding
        ),
        preparing=ReleaseState.REDIS_PREPARING,
        binding_key="redis_binding",
        phase_key="redis_phase",
        validate=validate,
        step=lambda conn: rt.redis.step(conn),
        start_message="앱 전용 Memorystore·연결 Secret을 준비합니다. 기존 트래픽은 유지합니다.",
        ready_message="Redis 준비 완료. REDIS_URL과 TLS CA·사설 연결을 후보 버전에 적용합니다.",
        phase_message=REDIS_PHASE_MESSAGES.__getitem__,
        conflict_message="Redis 준비 작업의 소유권·실행 이력을 확인해야 합니다.",
        failure_suffix=" 기존 앱 트래픽과 데이터·캐시는 보존했습니다.",
        error_code="gcp_redis_preparation_failed",
    )


def provision_stages(ctx: StepContext) -> list[ProvisionStage]:
    builders = (data_stage, cache_stage, redis_stage)
    return [stage for stage in (build(ctx) for build in builders) if stage.applies]


def run_migration(ctx: StepContext) -> bool:
    cp = ctx.cp
    if ctx.state == ReleaseState.QUEUED and ctx.request.migration and not cp.get("migration_done"):
        _start_migration(ctx)
        return True
    if ctx.state == ReleaseState.MIGRATING:
        _step_migration(ctx)
        return True
    return False


def _start_migration(ctx: StepContext) -> None:
    if ctx.runtime.migrate is None:
        ctx.move(
            ReleaseState.FAILED,
            "이 환경에는 Migration Job 실행 설정이 없습니다.",
            error_code="migration_unconfigured",
        )
        return
    ctx.move(
        ReleaseState.MIGRATING,
        "DB Migration Job을 실행합니다. 기존 앱 트래픽은 유지됩니다.",
    )


def _step_migration(ctx: StepContext) -> None:
    cp = ctx.cp
    try:
        phase, done = ctx.runtime.migrate.step(
            ctx.conn, ctx.row, cp, ctx.request, ctx.provider.data_binding
        )
    except MigrationError as exc:
        ctx.move(ReleaseState.FAILED, str(exc), error_code="migration_failed")
        return
    if done:
        cp["migration_done"] = done
        ctx.move(ReleaseState.QUEUED, "DB Migration 완료. 후보 버전을 배포합니다.")
    elif cp.get("migration_phase") != phase:
        cp["migration_phase"] = phase
        ctx.move(ReleaseState.MIGRATING, MIGRATION_PHASE_MESSAGES[phase])
    else:
        ctx.poll()
