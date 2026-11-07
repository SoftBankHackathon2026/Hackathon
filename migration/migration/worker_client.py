"""INSERT 워커 제어 API(8080) 의 HTTP 클라이언트."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class WorkerClientError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body or {}


class HttpWorkerClient:
    """loadgen.py 의 /status /pause /resume /drain /target 를 호출한다."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"error": raw[:300]}
            raise WorkerClientError(f"{method} {path} → HTTP {exc.code}: {parsed.get('error', raw[:300])}",
                                    status=exc.code, body=parsed if isinstance(parsed, dict) else None) from None
        except urllib.error.URLError as exc:
            raise WorkerClientError(f"{method} {path} 연결 실패: {exc.reason}") from None

    def healthz(self) -> dict[str, Any]:
        return self._call("GET", "/healthz")

    def status(self) -> dict[str, Any]:
        return self._call("GET", "/status")

    def pause(self) -> dict[str, Any]:
        return self._call("POST", "/pause")

    def resume(self) -> dict[str, Any]:
        return self._call("POST", "/resume")

    def set_generation_limit(self, limit: int) -> dict[str, Any]:
        return self._call("POST", "/generation-limit", {"limit": limit})

    def drain(self, timeout: float = 60.0) -> dict[str, Any]:
        try:
            return self._call("POST", "/drain", {"timeout": timeout}, timeout=timeout + 10)
        except WorkerClientError as exc:
            if exc.status == 409 and exc.body:
                return exc.body  # settled=False 응답
            raise

    def switch_target(self, target: str, phase: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"target": target}
        if phase:
            body["phase"] = phase
        return self._call("POST", "/target", body)
