"""보고. 항상 로그/stdout 에 남기고, SLACK_WEBHOOK_URL 이 있으면 웹훅으로, SLACK_BOT_TOKEN 과 채널이 있으면 chat.postMessage 로도 보낸다. 보고 실패가 복구 흐름을 멈추지 않는다."""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Mapping, Protocol

log = logging.getLogger("agent.notify")
SLACK_URL = "https://slack.com/api/chat.postMessage"


class Notifier(Protocol):
    def send(self, text: str, blocks: list[dict[str, Any]] | None = None) -> None: ...


class StdoutNotifier:
    def send(self, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        print(text, file=sys.stdout, flush=True)


class SlackNotifier:
    def __init__(self, token: str, channel: str, *, timeout: float = 5.0, thread_ts: str | None = None) -> None:
        self._token, self.channel, self._timeout, self.thread_ts = token, channel, timeout, thread_ts

    def __repr__(self) -> str:
        return f"SlackNotifier(channel={self.channel!r})"

    def send(self, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        payload: dict[str, Any] = {"channel": self.channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        if self.thread_ts:
            payload["thread_ts"] = self.thread_ts
        body = json.dumps(payload).encode("utf-8")
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


WEBHOOK_PREFIX = "https://hooks.slack.com/services/"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class SlackWebhookNotifier:
    def __init__(self, url: str, *, timeout: float = 5.0, thread_ts: str | None = None) -> None:
        if not url.startswith(WEBHOOK_PREFIX):
            raise ValueError("Slack Incoming Webhook 주소가 아니다")
        self._url, self._timeout, self.thread_ts = url, timeout, thread_ts
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def __repr__(self) -> str:
        return "SlackWebhookNotifier()"

    def send(self, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        payload: dict[str, Any] = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        if self.thread_ts:
            payload["thread_ts"] = self.thread_ts
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self._url, data=body, method="POST", headers={
            "Content-Type": "application/json; charset=utf-8",
        })
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                answer = resp.read(64).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            exc.close()
            log.warning("Slack 웹훅 거절: HTTP %s", exc.code)
            return
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            log.warning("Slack 웹훅 전송 실패: %s", type(exc).__name__)
            return
        if answer.strip() != "ok":
            log.warning("Slack 웹훅 응답이 ok 가 아니다")


class CompositeNotifier:
    def __init__(self, *notifiers: Notifier) -> None:
        self.notifiers = notifiers

    def send(self, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        for notifier in self.notifiers:
            notifier.send(text, blocks)


def build_notifier(channel: str | None, env: Mapping[str, str] | None = None, thread_ts: str | None = None) -> Notifier:
    environ = os.environ if env is None else env
    webhook = environ.get("SLACK_WEBHOOK_URL")
    if webhook:
        try:
            return CompositeNotifier(StdoutNotifier(), SlackWebhookNotifier(webhook, thread_ts=thread_ts))
        except ValueError as exc:
            log.warning("%s. stdout 으로만 보고한다", exc)
    token = environ.get("SLACK_BOT_TOKEN")
    if token and channel:
        return CompositeNotifier(StdoutNotifier(), SlackNotifier(token, channel, thread_ts=thread_ts))
    if token:
        log.warning("SLACK_BOT_TOKEN 은 있지만 slack.channel 이 없어 stdout 으로만 보고한다")
    return StdoutNotifier()
