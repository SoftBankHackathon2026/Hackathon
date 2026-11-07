"""보고. 항상 로그/stdout 에 남기고, SLACK_BOT_TOKEN 이 있으면 chat.postMessage 도 보낸다. 보고 실패가 복구 흐름을 멈추지 않는다."""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from typing import Mapping, Protocol

log = logging.getLogger("agent.notify")
SLACK_URL = "https://slack.com/api/chat.postMessage"


class Notifier(Protocol):
    def send(self, text: str) -> None: ...


class StdoutNotifier:
    def send(self, text: str) -> None:
        print(text, file=sys.stdout, flush=True)


class SlackNotifier:
    def __init__(self, token: str, channel: str, *, timeout: float = 5.0) -> None:
        self._token, self.channel, self._timeout = token, channel, timeout

    def __repr__(self) -> str:
        return f"SlackNotifier(channel={self.channel!r})"

    def send(self, text: str) -> None:
        body = json.dumps({"channel": self.channel, "text": text}).encode("utf-8")
        req = urllib.request.Request(SLACK_URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {self._token}", "Content-Type": "application/json; charset=utf-8",
        })
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 - 고정 주소
                doc = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            log.warning("Slack 전송 실패: %s", type(exc).__name__)
            return
        if not doc.get("ok"):
            log.warning("Slack 전송 거절: %s", doc.get("error"))


class CompositeNotifier:
    def __init__(self, *notifiers: Notifier) -> None:
        self.notifiers = notifiers

    def send(self, text: str) -> None:
        for notifier in self.notifiers:
            notifier.send(text)


def build_notifier(channel: str | None, env: Mapping[str, str] | None = None) -> Notifier:
    environ = os.environ if env is None else env
    token = environ.get("SLACK_BOT_TOKEN")
    if token and channel:
        return CompositeNotifier(StdoutNotifier(), SlackNotifier(token, channel))
    if token:
        log.warning("SLACK_BOT_TOKEN 은 있지만 slack.channel 이 없어 stdout 으로만 보고한다")
    return StdoutNotifier()
