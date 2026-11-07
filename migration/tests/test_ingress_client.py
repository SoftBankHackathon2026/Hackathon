"""ingress_client: 제어 API 호출 형식과 대상 앱 readiness 판정을 로컬 HTTP 서버로 검증한다."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from migration.ingress_client import HttpIngressClient, IngressClientError, check_app_ready


class _Handler(BaseHTTPRequestHandler):
    state = {"mode": "running", "target": "onprem", "epoch": "0", "in_flight": 0, "last_error": None}
    seen: list[tuple[str, str, dict, str | None]] = []
    info = {"environment": "aws", "database": {"name": "migration_demo"}, "writes_paused": False, "version": "x"}
    require_token: str | None = None

    def log_message(self, *a):  # noqa: D401
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        auth = self.headers.get("Authorization")
        _Handler.seen.append(("GET", self.path, {}, auth))
        if self.path == "/status":
            return self._send(200, _Handler.state)
        if self.path == "/readyz":
            return self._send(200, {"status": "ok"})
        if self.path == "/api/info":
            return self._send(200, _Handler.info)
        return self._send(404, {"error": "NOT_FOUND"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        auth = self.headers.get("Authorization")
        _Handler.seen.append(("POST", self.path, body, auth))
        if _Handler.require_token and auth != f"Bearer {_Handler.require_token}":
            return self._send(401, {"error": "CONTROL_UNAUTHORIZED"})
        st = _Handler.state
        if self.path == "/pause":
            st["mode"] = "paused"; return self._send(200, {**st, "settled": True})
        if self.path == "/target":
            if st["mode"] != "paused":
                return self._send(409, {"error": "PAUSE_REQUIRED"})
            if body.get("expected_epoch") != st["epoch"]:
                return self._send(409, {"error": "EPOCH_CONFLICT"})
            st["target"] = body["target"]; st["epoch"] = str(int(st["epoch"]) + 1); return self._send(200, st)
        if self.path == "/resume":
            if body.get("expected_epoch") != st["epoch"]:
                return self._send(409, {"error": "EPOCH_CONFLICT"})
            st["mode"] = "running"; return self._send(200, st)
        return self._send(404, {"error": "NOT_FOUND"})


@pytest.fixture
def server():
    _Handler.state = {"mode": "running", "target": "onprem", "epoch": "0", "in_flight": 0, "last_error": None}
    _Handler.seen = []
    _Handler.require_token = None
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_control_sequence_uses_epoch(server):
    c = HttpIngressClient(server)
    assert c.status()["target"] == "onprem"
    with pytest.raises(IngressClientError, match="PAUSE_REQUIRED"):
        c.set_target("aws_db", "0")
    assert c.pause()["settled"] is True
    res = c.set_target("aws_db", "0")
    assert res["target"] == "aws_db" and res["epoch"] == "1"
    with pytest.raises(IngressClientError, match="EPOCH_CONFLICT"):
        c.resume("0")
    assert c.resume("1")["mode"] == "running"
    assert ("POST", "/target", {"target": "aws_db", "expected_epoch": "0"}, None) in _Handler.seen


def test_bearer_token_from_env_only(server):
    _Handler.require_token = "s3cret"
    with pytest.raises(IngressClientError, match="INGRESS_CONTROL_TOKEN"):
        HttpIngressClient(server, token_env="INGRESS_CONTROL_TOKEN", env={})
    c = HttpIngressClient(server, token_env="INGRESS_CONTROL_TOKEN", env={"INGRESS_CONTROL_TOKEN": "s3cret"})
    assert c.pause()["settled"] is True
    assert _Handler.seen[-1][3] == "Bearer s3cret"
    bad = HttpIngressClient(server, token_env="T", env={"T": "nope"})
    with pytest.raises(IngressClientError, match="401"):
        bad.pause()


def test_connection_refused_is_client_error():
    with pytest.raises(IngressClientError, match="연결 실패"):
        HttpIngressClient("http://127.0.0.1:1", timeout=1).status()


def test_app_ready_judgement(server):
    ok = check_app_ready(server, environment="aws", database="migration_demo")
    assert ok["ok"] and ok["problems"] == []
    _Handler.info = {"environment": "onprem", "database": {"name": "migration_demo_return"}, "writes_paused": True}
    bad = check_app_ready(server, environment="aws", database="migration_demo")
    assert not bad["ok"]
    assert any("environment=onprem" in p for p in bad["problems"])
    assert any("database=migration_demo_return" in p for p in bad["problems"])
    assert any("writes_paused" in p for p in bad["problems"])
    _Handler.info = {"environment": "aws", "database": {"name": "migration_demo"}, "writes_paused": False}
