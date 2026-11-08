"""Slack 메시지 버튼으로 에이전트 승인·거절을 받는다.

Slack 은 버튼을 누르면 서명한 요청을 보낸다. 서명을 확인하고, 플랫폼 화면 버튼과 같은 승인 대기열에 넣는다.
에이전트는 지금처럼 플랫폼에서 결정을 가져가 처리한다. 응답은 Slack 이 준 response_url 로만 보낸다.
"""

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import SecretStr

from controlplane.core.secrets import secret_from_environment
from controlplane.schemas.agent import RUN_ID_PATTERN, Decision

MAX_SKEW_SECONDS = 300
MAX_BODY_BYTES = 64 * 1024
RESPONSE_HOST = "hooks.slack.com"
ACTIONS: dict[str, Decision] = {"agent_approve": "approve", "agent_reject": "reject"}
DECIDED = {"approve": "승인", "reject": "거절"}
_RUN_ID = re.compile(RUN_ID_PATTERN)
_NAME_CHARS = re.compile(r"[^\w.\- ]")


@dataclass(frozen=True)
class SlackApproval:
    signing_secret: SecretStr
    approvers: frozenset[str]

    def verify(self, body: bytes, timestamp: str | None, signature: str | None, now: float) -> bool:
        if not timestamp or not signature or not timestamp.isdigit():
            return False
        if abs(now - int(timestamp)) > MAX_SKEW_SECONDS:
            return False
        base = b"v0:" + timestamp.encode() + b":" + body
        digest = hmac.new(self.signing_secret.get_secret_value().encode(), base, hashlib.sha256)
        return hmac.compare_digest("v0=" + digest.hexdigest(), signature)

    def allows(self, user_id: str) -> bool:
        return not self.approvers or user_id in self.approvers


@dataclass(frozen=True)
class SlackDecision:
    run_id: str
    decision: Decision
    user_id: str
    user_name: str
    response_url: str
    blocks: list[dict[str, Any]]

    @property
    def requested_by(self) -> str:
        name = _NAME_CHARS.sub("", self.user_name).strip()[:40] or self.user_id
        return f"{name}, Slack"


def slack_approval_from_environment() -> SlackApproval | None:
    secret = secret_from_environment("ONEPORT_SLACK_SIGNING_SECRET")
    if not secret.get_secret_value():
        return None
    approvers = os.getenv("ONEPORT_SLACK_APPROVERS", "")
    return SlackApproval(secret, frozenset(a.strip() for a in approvers.split(",") if a.strip()))


def read_payload(body: bytes) -> dict[str, Any] | None:
    try:
        raw = parse_qs(body.decode("utf-8"), strict_parsing=True).get("payload", [""])[0]
        payload = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def read_decision(payload: dict[str, Any]) -> SlackDecision | None:
    if payload.get("type") != "block_actions":
        return None
    actions = payload.get("actions")
    user = payload.get("user")
    if not isinstance(actions, list) or not actions or not isinstance(user, dict):
        return None
    action = actions[0] if isinstance(actions[0], dict) else {}
    decision = ACTIONS.get(str(action.get("action_id")))
    run_id = str(action.get("value") or "")
    user_id = str(user.get("id") or "")
    response_url = str(payload.get("response_url") or "")
    if (
        decision is None
        or not _RUN_ID.fullmatch(run_id)
        or not user_id
        or not safe_response_url(response_url)
    ):
        return None
    message = payload.get("message")
    blocks = message.get("blocks") if isinstance(message, dict) else None
    return SlackDecision(
        run_id=run_id,
        decision=decision,
        user_id=user_id,
        user_name=str(user.get("name") or user.get("username") or ""),
        response_url=response_url,
        blocks=[b for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else [],
    )


def safe_response_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == RESPONSE_HOST and parts.port is None


def decided_message(decision: SlackDecision) -> dict[str, Any]:
    kept = [block for block in decision.blocks if block.get("type") != "actions"]
    note = (
        f"<@{decision.user_id}> 님이 {DECIDED[decision.decision]}했어요. "
        "에이전트가 다음 확인 때 처리해요."
    )
    return {
        "replace_original": True,
        "text": note,
        "blocks": [*kept, {"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}],
    }


def notice(text: str) -> dict[str, Any]:
    return {"response_type": "ephemeral", "replace_original": False, "text": text}
