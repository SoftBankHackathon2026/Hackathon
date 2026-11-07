"""트래픽봇의 영속 생성 제어 API. DB 대상은 이 API 로 바꾸지 않는다."""
from __future__ import annotations

import os
from typing import Any

from .config import BotConfig
from .ingress_client import _http_json, IngressClientError


class BotClientError(RuntimeError):
    pass


class HttpBotClient:
    def __init__(self, config: BotConfig, env: dict[str, str] | None = None) -> None:
        self.url = config.control_url.rstrip("/")
        self.timeout = config.timeout_seconds
        self.token = (os.environ if env is None else env).get(config.token_env)
        if not self.url or not self.token:
            raise BotClientError(f"bot.control_url 및 {config.token_env} 설정 필요")

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            status, out = _http_json(method, self.url + path, body, timeout=self.timeout, token=self.token)
        except IngressClientError:
            raise BotClientError(f"트래픽봇 {method} {path} 연결 실패") from None
        if status != 200:
            # Do not echo upstream bodies or bearer tokens into the job checkpoint.
            raise BotClientError(f"트래픽봇 {method} {path} HTTP {status}")
        return out

    def status(self) -> dict[str, Any]:
        return self._call("GET", "/control")

    def set_generation(self, enabled: bool, *, expected_run_id: str, expected_ledger_id: str,
                       limit: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"enabled": enabled, "expected_run_id": expected_run_id,
                                "expected_ledger_id": expected_ledger_id}
        if limit is not None:
            body["limit"] = limit
        return self._call("POST", "/control/generation", body)
