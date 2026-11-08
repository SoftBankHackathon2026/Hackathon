import time

import httpx
from fastapi import APIRouter, BackgroundTasks, Request, Response

from controlplane.api.deps import ContainerDep
from controlplane.core.errors import NotConfigured, OperationNotFound, RequestConflict
from controlplane.services.slack_approval import (
    MAX_BODY_BYTES,
    decided_message,
    notice,
    read_decision,
    read_payload,
)

router = APIRouter(prefix="/api/v1", tags=["slack"])

SLACK_NOT_CONFIGURED = "Slack 승인이 설정되지 않았어요. ONEPORT_SLACK_SIGNING_SECRET 을 설정하세요."
NOT_ALLOWED = "승인할 수 있는 사람이 아니에요. 플랫폼 담당자에게 요청하세요."
AGENT_OFF = "플랫폼에 에이전트 기록 저장소가 없어 결정을 받을 수 없어요."
RUN_GONE = "해당 실행 기록을 찾지 못했어요."


def reply(url: str, body: dict) -> None:
    try:
        httpx.post(url, json=body, timeout=5.0, follow_redirects=False)
    except httpx.HTTPError:
        pass


@router.post("/slack/interactions")
async def interactions(
    request: Request, background: BackgroundTasks, container: ContainerDep
) -> Response:
    slack = container.slack_approval
    if slack is None:
        raise NotConfigured(SLACK_NOT_CONFIGURED)
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return Response(status_code=413)
    if not slack.verify(
        body,
        request.headers.get("X-Slack-Request-Timestamp"),
        request.headers.get("X-Slack-Signature"),
        time.time(),
    ):
        return Response(status_code=401)
    payload = read_payload(body)
    decision = read_decision(payload) if payload else None
    if decision is None:
        return Response(status_code=200)
    if not slack.allows(decision.user_id):
        answer = notice(NOT_ALLOWED)
    elif container.agent is None:
        answer = notice(AGENT_OFF)
    else:
        try:
            container.agent.decide_as(decision.run_id, decision.decision, decision.requested_by)
            answer = decided_message(decision)
        except RequestConflict as exc:
            answer = notice(exc.message)
        except OperationNotFound:
            answer = notice(RUN_GONE)
    background.add_task(reply, decision.response_url, answer)
    return Response(status_code=200)
