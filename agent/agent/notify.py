"""알림. Slack 은 chat.postMessage 만 쓰므로 slack_bolt 없이 urllib 로 보낸다. 봇 토큰은 환경변수."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Protocol

from .config import env_secret

log = logging.getLogger("agent.notify")


class Notifier(Protocol):
    def post(self, text: str, *, blocks: list[dict[str, Any]] | None = None, thread_ts: str | None = None) -> str | None:
        """메시지를 보내고 thread 용 ts 를 돌려준다 (없으면 None)."""


class LogNotifier:
    """Slack 이 없을 때. 보낸 메시지를 기억해 테스트에서 확인한다."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def post(self, text: str, *, blocks: list[dict[str, Any]] | None = None, thread_ts: str | None = None) -> str | None:
        self.messages.append(text)
        log.info("[notify] %s", text)
        return None


class SlackNotifier:
    """chat.postMessage. 실패해도 예외를 밖으로 던지지 않는다 — 알림 실패가 수정 흐름을 막으면 안 된다."""

    def __init__(self, channel: str, *, token: str | None = None, token_env: str = "SLACK_BOT_TOKEN",
                 timeout: float = 5.0, api_url: str = "https://slack.com/api/chat.postMessage") -> None:
        self.channel = channel
        self._token = token if token is not None else env_secret(token_env)
        self.timeout = timeout
        self.api_url = api_url
        if not self._token:
            raise ValueError(f"{token_env} 환경변수가 없다")

    def post(self, text: str, *, blocks: list[dict[str, Any]] | None = None, thread_ts: str | None = None) -> str | None:
        body: dict[str, Any] = {"channel": self.channel, "text": text}
        if blocks:
            body["blocks"] = blocks
        if thread_ts:
            body["thread_ts"] = thread_ts
        req = urllib.request.Request(
            self.api_url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {self._token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                doc = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            log.error("Slack 전송 실패: %s", exc)
            return None
        if not doc.get("ok"):
            log.error("Slack 오류: %s", doc.get("error"))
            return None
        return doc.get("ts")


def section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}
