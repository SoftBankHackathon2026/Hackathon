"""접수 서비스(durable-ingress) 제어 API(9092) 와 대상 앱 readiness 의 HTTP 클라이언트.

cutover 는 이 클라이언트로 접수 큐를 워커와 같은 순서로 제어한다:
FENCING 에서 pause(settled), SWITCHING 에서 대상 앱 readiness 확인 → target → resume.
제어 API 는 epoch 로 보호되므로 모든 변경 호출은 직전에 읽은 epoch 를 함께 보낸다.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class IngressClientError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body or {}


def _http_json(method: str, url: str, body: dict[str, Any] | None = None, *, timeout: float,
               token: str | None = None) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8") or "{}"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw[:300]}
            return resp.status, parsed if isinstance(parsed, dict) else {"value": parsed}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"error": raw[:300]}
        return exc.code, parsed if isinstance(parsed, dict) else {"error": str(parsed)[:300]}
    except urllib.error.URLError as exc:
        raise IngressClientError(f"{method} {url} 연결 실패: {exc.reason}") from None


class HttpIngressClient:
    """durable-ingress 의 /status /pause /target /resume 를 호출한다.

    token_env 가 설정되어 있고 그 환경변수가 비어 있지 않으면 Bearer 로 보낸다. 토큰 값은 로그에 남기지 않는다.
    """

    def __init__(self, control_url: str, *, token_env: str | None = None, timeout: float = 30.0,
                 env: dict[str, str] | None = None) -> None:
        self.control_url = control_url.rstrip("/")
        self.timeout = timeout
        source = os.environ if env is None else env
        self._token = source.get(token_env) if token_env else None
        if token_env and not self._token:
            raise IngressClientError(f"환경변수 {token_env} 가 비어 있다 (접수 서비스 제어 토큰)")

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        status, parsed = _http_json(method, self.control_url + path, body, timeout=self.timeout, token=self._token)
        if status >= 400:
            raise IngressClientError(f"{method} {path} → HTTP {status}: {parsed.get('error') or parsed}",
                                     status=status, body=parsed)
        return parsed

    def status(self) -> dict[str, Any]:
        return self._call("GET", "/status")

    def pause(self) -> dict[str, Any]:
        """새 전달을 멈추고 진행 중 1건이 끝날 때까지 기다린 뒤 settled:true 를 돌려준다."""
        return self._call("POST", "/pause", {})

    def set_target(self, target: str, expected_epoch: str) -> dict[str, Any]:
        return self._call("POST", "/target", {"target": target, "expected_epoch": str(expected_epoch)})

    def resume(self, expected_epoch: str) -> dict[str, Any]:
        return self._call("POST", "/resume", {"expected_epoch": str(expected_epoch)})


def check_app_ready(base_url: str, *, environment: str | None = None, database: str | None = None,
                    writes_open: bool = True, timeout: float = 10.0,
                    token_env: str | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    """대상 앱의 /readyz 와 /api/info 를 읽어 전환해도 되는지 판정한다.

    sample-app `scripts/preflight-live-migration.js` 와 같은 기준이다:
    /readyz 200 + status ok, /api/info 의 environment·database.name 이 기대와 같고 writes_paused 가 아니어야 한다.
    """
    base = base_url.rstrip("/")
    source = os.environ if env is None else env
    token = source.get(token_env) if token_env else None
    out: dict[str, Any] = {"url": base, "ok": True, "problems": []}
    status, ready = _http_json("GET", base + "/readyz", timeout=timeout, token=token)
    out["readyz"] = {"status": status, "body_status": ready.get("status")}
    if status != 200 or ready.get("status") != "ok":
        out["problems"].append(f"/readyz {status} {ready.get('status')}")
    status, info = _http_json("GET", base + "/api/info", timeout=timeout, token=token)
    db = info.get("database") if isinstance(info.get("database"), dict) else {}
    out["info"] = {"status": status, "environment": info.get("environment"), "database": db.get("name"),
                   "writes_paused": info.get("writes_paused"), "version": info.get("version")}
    if status != 200:
        out["problems"].append(f"/api/info {status}")
    if environment and info.get("environment") != environment:
        out["problems"].append(f"environment={info.get('environment')} (기대 {environment})")
    if database and db.get("name") != database:
        out["problems"].append(f"database={db.get('name')} (기대 {database})")
    if writes_open and info.get("writes_paused") is True:
        out["problems"].append("writes_paused=true (앱 쓰기가 아직 닫혀 있다)")
    out["ok"] = not out["problems"]
    return out
