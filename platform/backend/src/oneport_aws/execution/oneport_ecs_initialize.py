from typing import Any

from controlplane.domain.models import Runtime
from oneport_aws.execution.oneport_ecs_registration import check_registration
from oneport_aws.execution.oneport_ecs_release import ReleaseContext, ReleaseState


def initialize_release(ctx: ReleaseContext) -> None:
    request, provider = ctx.request, ctx.provider
    app = ctx.registry.get(request.application, request.environment_id)
    if app is None:
        ctx.move(
            ReleaseState.FAILED,
            "등록되지 않은 앱입니다. operator register를 먼저 실행하세요.",
            error_code="app_not_registered",
        )
        return
    rejection = check_registration(app, request)
    if rejection:
        ctx.move(ReleaseState.FAILED, rejection.message, error_code=rejection.code)
        return
    priority = app.rule_priority
    if provider.host_rule() is None:
        priority = ctx.registry.reserve_priority(
            provider.environment, provider.listener_priorities()
        )
    ctx.cp["routing"] = provider.initialize(request, rule_priority=priority)
    ctx.cp["previous"] = _previous_release(ctx, provider.live_release())
    ctx.move(ReleaseState.INITIALIZED, "앱 리소스와 현재 운영 릴리즈를 기록했습니다.")


def _previous_release(ctx: ReleaseContext, live: str | None) -> dict[str, Any] | None:
    if not live:
        return None
    request = ctx.request
    previous = ctx.conn.execute(
        """SELECT * FROM releases WHERE id=%s AND application=%s AND environment_id=%s
           AND runtime=%s AND state='succeeded' AND binding_fingerprint=%s""",
        (
            live,
            request.application,
            request.environment_id,
            Runtime.AWS_ECS,
            ctx.row["binding_fingerprint"],
        ),
    ).fetchone()
    ctx.conn.commit()
    if previous:
        return {"id": previous["id"], "request": previous["request"]}
    return {"id": live, "request": None}
