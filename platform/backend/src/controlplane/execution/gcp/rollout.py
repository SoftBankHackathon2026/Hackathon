"""Configured canary/blue-green steps persisted before Cloud Run mutations.

Time controls promotion; health checks only gate initial verification and optional
rollback. No traffic-percentage decisions are inferred from Monitoring metrics.
"""

from enum import StrEnum

from controlplane.domain.models import Strategy
from controlplane.execution.gcp.context import StepContext
from controlplane.execution.gcp.states import ReleaseState
from controlplane.providers.contracts import ProviderConflict
from controlplane.providers.gcp.provider import RevisionState
from controlplane.providers.gcp.traffic import digest, normalized, targets, traffic

SOAK_STATES = frozenset(
    {ReleaseState.HOLDING, ReleaseState.AWAITING_PROMOTION, ReleaseState.BAKING}
)

TRAFFIC_MESSAGES = {
    ReleaseState.BAKING: "새 버전 100%. 전환 후 응답 확인.",
    ReleaseState.RESTORING: "기존 버전 100%. 복구 응답 확인.",
}


class Promotion(StrEnum):
    MANUAL = "manual"
    AUTO = "auto"


def begin_switch(ctx: StepContext, percent: int, next_state: ReleaseState) -> None:
    cp = ctx.cp
    cp.update(
        desired=targets(cp["stable"], cp["candidate"], percent),
        percent=percent,
        next=next_state,
    )
    ctx.move(ReleaseState.SWITCHING, f"새 버전 {percent}% 전환 의도를 저장했습니다.")


def begin_restore(ctx: StepContext, reason: str) -> None:
    cp = ctx.cp
    cp.pop("action", None)
    cp.update(
        desired=cp["rollback_traffic"],
        next=ReleaseState.RESTORING,
        rollback_reason=reason,
        percent=0,
    )
    ctx.move(ReleaseState.SWITCHING, "이전 버전 트래픽 복구 의도를 저장했습니다.")


def fail_candidate(ctx: StepContext, reason: str) -> None:
    if ctx.request.rollout.auto_rollback:
        begin_restore(ctx, reason)
    else:
        ctx.move(
            ReleaseState.AWAITING_ROLLBACK,
            "앱 검증 실패. 자동 복귀가 꺼져 있어 수동 복귀를 기다립니다.",
            error_code=reason,
        )


def stable_request(ctx: StepContext):
    return ctx.request.model_copy(
        update={"health_path": ctx.cp.get("stable_health_path", ctx.request.health_path)}
    )


def reset_counters(cp: dict) -> None:
    cp["samples"], cp["attempts"], cp["failures"] = 0, 0, 0


def run_rollout(ctx: StepContext) -> None:
    handlers = {
        ReleaseState.QUEUED: _snapshot,
        ReleaseState.SUBMITTING: _submit,
        ReleaseState.WAITING: _wait_for_candidate,
        ReleaseState.VERIFYING: _verify_candidate,
        ReleaseState.SWITCHING: _switch,
        ReleaseState.TRAFFIC_WAITING: _wait_for_traffic,
        ReleaseState.HOLDING: _soak,
        ReleaseState.AWAITING_PROMOTION: _soak,
        ReleaseState.BAKING: _soak,
        ReleaseState.RESTORING: _verify_restore,
        ReleaseState.AWAITING_ROLLBACK: _await_rollback,
    }
    handler = handlers.get(ctx.state)
    if handler is None:
        raise ProviderConflict("Unknown Cloud Run rollout phase")
    handler(ctx)


def _stable_health_path(ctx: StepContext) -> str:
    request = ctx.request
    previous = ctx.conn.execute(
        "SELECT request FROM releases WHERE id=%s AND application=%s "
        "AND environment_id=%s AND runtime=%s",
        (
            ctx.cp["stable_release"],
            request.application,
            request.environment_id,
            ctx.worker.queue_runtime,
        ),
    ).fetchone()
    if previous:
        return previous["request"].get("health_path", request.health_path)
    return request.health_path


def _snapshot(ctx: StepContext) -> None:
    cp, provider, rt = ctx.cp, ctx.provider, ctx.runtime
    try:
        snapshot = provider.snapshot(ctx.request, ctx.release_id)
    except ProviderConflict:
        ctx.move(
            ReleaseState.FAILED,
            "기존 Cloud Run 서비스의 트래픽·버전 설정이 배포 조건과 맞지 않습니다. "
            "클라우드를 변경하지 않았습니다. 환경 설정을 확인하세요.",
            error_code="cloud_run_preflight_conflict",
        )
        return
    cp.update(snapshot)
    cp["stable_health_path"] = _stable_health_path(ctx)
    service = provider.read()
    if not rt.probe(
        stable_request(ctx),
        service,
        url=ctx.settings.environment.service_url,
        release=cp["stable_release"],
    ):
        ctx.move(
            ReleaseState.FAILED,
            "기존 서비스 응답 실패. 배포를 시작하지 않았습니다.",
            error_code="baseline_unhealthy",
        )
        return
    cp["current"] = targets(cp["stable"], cp["candidate"], 0)
    ctx.move(ReleaseState.SUBMITTING, "기존 버전 기록. 트래픽 0% 후보 생성 의도 저장.")


def _submit(ctx: StepContext) -> None:
    ctx.provider.submit(ctx.request, ctx.release_id, ctx.cp)
    ctx.move(ReleaseState.WAITING, "Cloud Run 후보 준비를 기다립니다.")


def _wait_for_candidate(ctx: StepContext) -> None:
    cp, provider = ctx.cp, ctx.provider
    service = provider.observe(cp, cp["current"])
    revision_state = provider.revision_state(provider.revision(cp["candidate"]))
    if revision_state == RevisionState.FAILED:
        fail_candidate(ctx, "candidate_not_ready")
    elif revision_state == RevisionState.READY and provider.converged(service, {cp["stable"]: 100}):
        reset_counters(cp)
        ctx.move(ReleaseState.VERIFYING, "후보 전용 URL에서 앱 응답을 확인합니다.")
    else:
        ctx.poll()


def _verify_candidate(ctx: StepContext) -> None:
    cp, provider, rt = ctx.cp, ctx.provider, ctx.runtime
    request, policy = ctx.request, ctx.request.rollout
    service = provider.observe(cp, cp["current"])
    healthy = rt.probe(
        request, service, url=provider.candidate_url(service, cp), release=ctx.release_id
    )
    cp["samples"] = cp.get("samples", 0) + 1 if healthy else 0
    cp["attempts"] = cp.get("attempts", 0) + 1
    if cp["samples"] >= ctx.settings.required_health_samples:
        cp["failures"] = 0
        _promote_verified(ctx, request.strategy, policy)
    elif cp["attempts"] >= ctx.settings.production_check_limit:
        fail_candidate(ctx, "candidate_unhealthy")
    else:
        ctx.poll()


def _promote_verified(ctx: StepContext, strategy: Strategy, policy) -> None:
    if strategy == Strategy.CANARY:
        begin_switch(ctx, policy.canary_percent, ReleaseState.HOLDING)
    elif policy.promotion == Promotion.MANUAL:
        ctx.move(ReleaseState.AWAITING_PROMOTION, "후보 검증 완료. 수동 전환을 기다립니다.")
    else:
        begin_switch(ctx, 100, ReleaseState.BAKING)


def _switch(ctx: StepContext) -> None:
    cp = ctx.cp
    ctx.provider.switch(cp, cp["current"], cp["desired"])
    ctx.move(ReleaseState.TRAFFIC_WAITING, "트래픽 설정 반영을 기다립니다.")


def _wait_for_traffic(ctx: StepContext) -> None:
    cp, provider, policy = ctx.cp, ctx.provider, ctx.request.rollout
    service = provider.read()
    if digest(service.template) != cp["candidate_template"]:
        raise ProviderConflict("Candidate changed during traffic update")
    observed = normalized(traffic(service))
    if observed not in (normalized(cp["current"]), normalized(cp["desired"])):
        raise ProviderConflict("Unexpected traffic during update")
    if cp["next"] == ReleaseState.RESTORING:
        desired_weights = {cp["stable"]: 100}
    else:
        desired_weights = {cp["stable"]: 100 - cp["percent"], cp["candidate"]: cp["percent"]}
    if observed != normalized(cp["desired"]) or not provider.converged(service, desired_weights):
        ctx.poll()
        return
    cp["current"] = cp["desired"]
    hold = cp["next"] == ReleaseState.HOLDING
    cp["until"] = ctx.runtime.now() + (policy.hold_seconds if hold else policy.bake_seconds)
    reset_counters(cp)
    next_state = ReleaseState(cp["next"])
    message = (
        f"새 버전 {cp['percent']}%. 설정한 유지 시간 시작."
        if hold
        else TRAFFIC_MESSAGES[next_state]
    )
    ctx.move(next_state, message)


def _soak(ctx: StepContext) -> None:
    cp, provider, rt = ctx.cp, ctx.provider, ctx.runtime
    policy, state = ctx.request.rollout, ctx.state
    service = provider.observe(cp, cp["current"])
    if (policy.auto_rollback or state == ReleaseState.BAKING) and _soak_unhealthy(ctx, service):
        fail_candidate(ctx, "health_check_failed")
        return
    if state == ReleaseState.HOLDING and rt.now() >= cp["until"]:
        if policy.promotion == Promotion.MANUAL:
            ctx.move(ReleaseState.AWAITING_PROMOTION, "유지 시간 완료. 수동 전환을 기다립니다.")
        else:
            begin_switch(ctx, 100, ReleaseState.BAKING)
    elif (
        state == ReleaseState.BAKING
        and rt.now() >= cp["until"]
        and cp["samples"] >= ctx.settings.required_health_samples
    ):
        ctx.move(
            ReleaseState.SUCCEEDED,
            "새 버전 100% 전환과 운영 응답 확인 완료.",
            url=ctx.settings.environment.service_url,
        )
    else:
        ctx.poll()


def _soak_unhealthy(ctx: StepContext, service) -> bool:
    cp, provider, rt = ctx.cp, ctx.provider, ctx.runtime
    release = ctx.release_id
    healthy = rt.probe(
        ctx.request, service, url=provider.candidate_url(service, cp), release=release
    )
    if ctx.state == ReleaseState.BAKING:
        healthy = healthy and rt.probe(
            ctx.request, service, url=ctx.settings.environment.service_url, release=release
        )
    cp["failures"] = 0 if healthy else cp.get("failures", 0) + 1
    cp["samples"] = cp.get("samples", 0) + 1 if healthy else 0
    return cp["failures"] >= ctx.request.rollout.failure_threshold


def _verify_restore(ctx: StepContext) -> None:
    cp, provider, rt = ctx.cp, ctx.provider, ctx.runtime
    service = provider.observe(cp, cp["current"])
    healthy = provider.converged(service, {cp["stable"]: 100}) and rt.probe(
        stable_request(ctx),
        service,
        url=ctx.settings.environment.service_url,
        release=cp["stable_release"],
    )
    cp["samples"] = cp.get("samples", 0) + 1 if healthy else 0
    cp["attempts"] = cp.get("attempts", 0) + 1
    if cp["samples"] >= ctx.settings.required_health_samples:
        ctx.move(
            ReleaseState.FAILED,
            "이전 버전으로 트래픽 복구 확인. DB는 변경하지 않았습니다.",
            error_code=cp["rollback_reason"] + "_restored",
            url=ctx.settings.environment.service_url,
        )
    elif cp["attempts"] >= ctx.settings.production_check_limit:
        ctx.move(
            ReleaseState.UNCERTAIN,
            "복구 트래픽 설정 후 앱 응답을 확인하지 못했습니다.",
            error_code="restore_unverified",
        )
    else:
        ctx.poll()


def _await_rollback(ctx: StepContext) -> None:
    ctx.provider.observe(ctx.cp, ctx.cp["current"])
    ctx.poll()
