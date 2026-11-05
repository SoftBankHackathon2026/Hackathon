from controlplane.execution.gcp.context import StepContext
from controlplane.execution.gcp.states import ReleaseState
from controlplane.providers.gcp.provider import RevisionState


def first_step(ctx: StepContext) -> None:
    state = ctx.state
    if state == ReleaseState.QUEUED:
        _snapshot(ctx)
    elif state == ReleaseState.SUBMITTING:
        _submit(ctx)
    elif state in {ReleaseState.WAITING, ReleaseState.VERIFYING}:
        _verify(ctx)


def _snapshot(ctx):
    app = ctx.runtime.app
    ctx.cp.update(app.snapshot(ctx.conn, ctx.request, ctx.release_id))
    ctx.move(ReleaseState.SUBMITTING, "첫 Cloud Run 서비스 생성 의도를 저장했습니다.")


def _submit(ctx):
    ctx.runtime.app.submit(ctx.conn, ctx.cp)
    ctx.move(ReleaseState.WAITING, "첫 Cloud Run 버전 준비를 기다립니다.")


def _verify(ctx):
    rt, cp, provider = ctx.runtime, ctx.cp, ctx.provider
    service = rt.app.observe(ctx.conn, cp)
    if service is None:
        ctx.poll()
        return
    revision_state = provider.revision_state(provider.revision(cp["candidate"]))
    ready = revision_state == RevisionState.READY
    healthy = (
        ready
        and provider.converged(service, {cp["candidate"]: 100})
        and rt.probe(ctx.request, service, url=service.uri, release=ctx.release_id)
    )
    cp["samples"] = cp.get("samples", 0) + 1 if healthy else 0
    cp["attempts"] = cp.get("attempts", 0) + ready
    if cp["samples"] >= ctx.settings.required_health_samples:
        rt.app.cp["initialized"] = True
        rt.app.save(ctx.conn)
        ctx.move(
            ReleaseState.SUCCEEDED,
            "첫 버전이 정상입니다. 다음 배포부터 단계적 전환을 사용합니다.",
            url=service.uri,
        )
    elif (
        revision_state == RevisionState.FAILED
        or cp["attempts"] >= ctx.settings.production_check_limit
    ):
        ctx.move(
            ReleaseState.FAILED,
            "첫 버전 상태 확인에 실패했습니다. 이전 버전이 없어 복구할 대상은 없습니다.",
            error_code="initial_service_unhealthy",
        )
    else:
        ctx.poll()
