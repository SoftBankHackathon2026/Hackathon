import time

from controlplane.providers.contracts import ProviderConflict, ProviderRejected
from oneport_aws.execution.oneport_ecs_release import ReleaseContext, ReleaseState
from oneport_aws.providers.oneport_ecs import migration
from oneport_aws.providers.oneport_ecs.migration import MigrationState


def run_migration_step(ctx: ReleaseContext) -> None:
    handlers = {
        ReleaseState.MIGRATION_PREPARING: _register,
        ReleaseState.MIGRATION_SUBMITTING: _start,
        ReleaseState.MIGRATION_WAITING: _wait,
    }
    handler = handlers.get(ReleaseState(ctx.row["state"]))
    if handler is None:
        raise ProviderConflict("Unknown AWS migration state")
    try:
        handler(ctx)
    except ProviderRejected as exc:
        ctx.move(ReleaseState.FAILED, str(exc), error_code="migration_rejected")


def _register(ctx: ReleaseContext) -> None:
    ctx.cp["migration_td"] = migration.register(ctx.provider, ctx.request, ctx.key)
    ctx.move(ReleaseState.MIGRATION_SUBMITTING, "마이그레이션 실행 의도를 저장했습니다.")


def _start(ctx: ReleaseContext) -> None:
    task = migration.find_task(ctx.provider, ctx.key) or migration.run(
        ctx.provider, ctx.cp["migration_td"], ctx.key
    )
    ctx.cp["migration_task"], ctx.cp["migration_started_at"] = task, time.time()
    ctx.move(
        ReleaseState.MIGRATION_WAITING,
        f"마이그레이션을 실행합니다 (task {task.rsplit('/', 1)[-1]}).",
    )


def _wait(ctx: ReleaseContext) -> None:
    result = migration.status(ctx.provider, ctx.cp["migration_task"])
    if result.state == MigrationState.SUCCEEDED:
        ctx.move(
            ReleaseState.PREPARING,
            "마이그레이션이 끝났습니다(exit 0). Task Definition을 등록합니다.",
        )
    elif result.state == MigrationState.FAILED:
        ctx.move(
            ReleaseState.FAILED,
            f"마이그레이션 실패({result.detail}). 새 버전은 배포하지 않았고 운영은 그대로입니다.",
            error_code="migration_failed",
        )
    elif _timed_out(ctx):
        ctx.move(
            ReleaseState.UNCERTAIN,
            "마이그레이션 시간 초과. 운영은 그대로이며 task 종료 여부를 확인해야 합니다.",
            error_code="migration_timeout",
        )
    else:
        ctx.poll()


def _timed_out(ctx: ReleaseContext) -> bool:
    started = ctx.cp.get("migration_started_at", time.time())
    return time.time() - started > ctx.request.migration.timeout_seconds
