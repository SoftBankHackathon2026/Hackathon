from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

from .config import TrafficConfig
from .ingress_client import IngressClientError, _http_json


class TrafficClientError(RuntimeError):
    pass


class HttpTrafficClient:
    def __init__(self, config: TrafficConfig, env: dict[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        base = (env.get(config.platform_url_env) or "").rstrip("/")
        token = env.get(config.token_env) or ""
        if not base or not token:
            raise TrafficClientError(f"공개 도메인 전환에는 {config.platform_url_env}, {config.token_env} 가 필요하다")
        assert config.app
        self.url = f"{base}/api/v1/ingest/traffic/{quote(config.app, safe='')}"
        self.token = token
        self.timeout = config.timeout_seconds

    def _call(self, method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            status, parsed = _http_json(method, self.url, body, timeout=self.timeout, token=self.token)
        except IngressClientError as exc:
            raise TrafficClientError(str(exc)) from None
        if status >= 400:
            raise TrafficClientError(f"{method} 공개 도메인 전환 API {status}: {parsed.get('detail') or parsed}")
        return parsed

    def status(self) -> dict[str, Any]:
        return self._call("GET")

    def switch(self, target: str) -> dict[str, Any]:
        return self._call("POST", {"target": target})
