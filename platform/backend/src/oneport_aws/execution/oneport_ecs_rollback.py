from controlplane.domain.models import DeploymentRequest
from controlplane.providers.contracts import Phase, ProviderRejected, ReleaseKey, Submission
from oneport_aws.execution.oneport_ecs_release import ReleaseContext, ReleaseState


def request_rollback(ctx: ReleaseContext) -> None:
    task_definition = ctx.cp["prepared"]["checkpoint"]["task_definition_arn"]
    try:
        ctx.cp["rollback"] = ctx.provider.rollback(task_definition).model_dump(mode="json")
    except ProviderRejected as exc:
        ctx.move(
            ReleaseState.UNCERTAIN,
            f"{exc} (operator restore --release <이전 성공 ID>로 재배포)",
            error_code="rollback_window_closed",
        )
        return
    ctx.cp["restore_samples"], ctx.cp["restore_attempts"] = 0, 0
    ctx.move(ReleaseState.RESTORE_CHECKING, "ECS 롤백을 요청했습니다. 이전 버전을 확인합니다.")


def check_rollback(ctx: ReleaseContext) -> None:
    rollback = Submission.model_validate(ctx.cp["rollback"])
    observed = ctx.provider.observe_rollback(rollback.resource_id)
    if observed.phase == Phase.FAILED:
        ctx.move(
            ReleaseState.UNCERTAIN,
            "ECS 롤백이 실패했습니다. 운영자 확인이 필요합니다.",
            error_code=observed.code,
        )
    elif observed.phase != Phase.READY:
        ctx.poll()
    else:
        _verify_previous(ctx)


def _verify_previous(ctx: ReleaseContext) -> None:
    previous = ctx.cp.get("previous")
    if not previous:
        ctx.move(
            ReleaseState.FAILED,
            "첫 배포 검증 실패. ECS가 서비스를 되돌렸습니다.",
            error_code="first_release_health_failed",
        )
        return
    if previous["request"] is None:
        ctx.move(
            ReleaseState.FAILED,
            "ECS가 이전 버전으로 되돌렸습니다(기록 없는 이전 버전, HTTP 미검증).",
            error_code="post_promotion_rolled_back_unverified",
        )
        return
    old = DeploymentRequest.model_validate(previous["request"])
    healthy = ctx.runtime.probe(old, ReleaseKey(value=previous["id"]))
    cp = ctx.cp
    cp["restore_samples"] = cp.get("restore_samples", 0) + 1 if healthy else 0
    cp["restore_attempts"] = cp.get("restore_attempts", 0) + 1
    settings = ctx.runtime.settings
    if cp["restore_samples"] >= settings.required_health_samples:
        ctx.move(
            ReleaseState.FAILED,
            "이전 릴리즈로 운영 트래픽과 HTTPS 응답을 복구했습니다.",
            error_code="post_promotion_rolled_back",
        )
    elif cp["restore_attempts"] >= settings.production_check_limit:
        ctx.move(
            ReleaseState.UNCERTAIN,
            "복구 후 HTTP 검증에 실패했습니다. 운영자 확인이 필요합니다.",
            error_code="rollback_http_unverified",
        )
    else:
        ctx.poll()
