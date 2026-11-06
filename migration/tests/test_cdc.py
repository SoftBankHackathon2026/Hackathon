"""cdc.py: 템플릿 렌더링과 Connect REST 클라이언트 (로컬 가짜 서버)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from migration.cdc import ConnectClient, ConnectError, load_connector_template_pair, mask_config, render_template, \
    sink_kafka_offsets, template_variables
from migration.config import ConfigError


def test_render_keeps_connect_secret_provider_syntax():
    text = '{"a": "{{job_id}}", "pw": "${file:/s.properties:MIGRATION_CDC_PASSWORD}", "t": "{{ table }}"}'
    out = render_template(text, {"job_id": "forward_001", "table": "public.migration_demo"})
    assert json.loads(out) == {"a": "forward_001", "pw": "${file:/s.properties:MIGRATION_CDC_PASSWORD}",
                               "t": "public.migration_demo"}


def test_render_missing_variable_is_error():
    with pytest.raises(ConfigError, match="nope"):
        render_template("{{nope}}", {})


def test_templates_render_from_config(config):
    docs = load_connector_template_pair("forward", template_variables(config))
    src, sink = docs["source"]["config"], docs["sink"]["config"]
    assert docs["source"]["name"] == "forward_001-source"
    assert src["slot.name"] == "forward_001" and src["publication.name"] == "forward_001_pub"
    assert src["topic.prefix"] == "forward_001" and src["publication.autocreate.mode"] == "disabled"
    assert src["snapshot.mode"] == "initial" and src["plugin.name"] == "pgoutput"
    assert src["table.include.list"] == "public.migration_demo"
    assert src["database.password"] == "${env:MIGRATION_CDC_PASSWORD}"
    assert sink["topics"] == "forward_001.public.migration_demo"
    assert sink["insert.mode"] == "upsert" and sink["primary.key.mode"] == "record_key"
    assert sink["primary.key.fields"] == "request_id" and sink["schema.evolution"] == "none"
    assert sink["tasks.max"] == "1" and sink["delete.enabled"] == "false"
    assert sink["connection.url"].startswith("jdbc:postgresql://aws-db:5432/migration_demo")
    assert mask_config(sink)["connection.password"] == "***"


def test_sink_offsets_parsing():
    doc = {"offsets": [{"partition": {"kafka_topic": "t", "kafka_partition": 0}, "offset": {"kafka_offset": 42}}]}
    assert sink_kafka_offsets(doc) == {"t-0": 42}
    assert sink_kafka_offsets(None) == {}


class _FakeConnect(BaseHTTPRequestHandler):
    connectors: dict[str, dict] = {}
    calls: list[tuple[str, str]] = []
    status_404_once: set[str] = set()

    def log_message(self, *_: object) -> None:
        pass

    def _json(self, code: int, body: object = None) -> None:
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self.calls.append(("GET", self.path))
        parts = self.path.strip("/").split("/")
        if self.path == "/connectors":
            return self._json(200, sorted(self.connectors))
        name = parts[1] if len(parts) > 1 else ""
        if name not in self.connectors:
            return self._json(404, {"error_code": 404, "message": "not found"})
        if len(parts) == 2:
            return self._json(200, {"name": name, "config": self.connectors[name]})
        if parts[2] == "config":
            return self._json(200, self.connectors[name])
        if parts[2] == "status":
            if name in self.status_404_once:
                self.status_404_once.discard(name)
                return self._json(404, {"error_code": 404, "message": f"No status found for connector {name}"})
            return self._json(200, {"name": name, "connector": {"state": "RUNNING"}, "tasks": [{"id": 0, "state": "RUNNING"}]})
        if parts[2] == "offsets":
            return self._json(200, {"offsets": [{"partition": {"kafka_topic": "t", "kafka_partition": 0}, "offset": {"kafka_offset": 7}}]})
        return self._json(404, {})

    def do_PUT(self):  # noqa: N802
        self.calls.append(("PUT", self.path))
        parts = self.path.strip("/").split("/")
        name = parts[1]
        if parts[-1] == "config":
            length = int(self.headers.get("Content-Length") or 0)
            self.connectors[name] = json.loads(self.rfile.read(length))
            return self._json(200, {"name": name, "config": self.connectors[name]})
        if parts[-1] == "stop":
            return self._json(405, {"message": "unsupported"})
        return self._json(202)

    def do_DELETE(self):  # noqa: N802
        self.calls.append(("DELETE", self.path))
        name = self.path.strip("/").split("/")[1]
        if name in self.connectors:
            del self.connectors[name]
            return self._json(204)
        return self._json(404, {"message": "not found"})


@pytest.fixture
def connect_server():
    _FakeConnect.connectors = {}
    _FakeConnect.calls = []
    _FakeConnect.status_404_once = set()
    server = HTTPServer(("127.0.0.1", 0), _FakeConnect)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_connect_client_lifecycle(connect_server):
    c = ConnectClient(connect_server, timeout=5)
    assert c.list_connectors() == []
    assert c.exists("x") is False
    c.put_config("x", {"a": "1"})
    c.put_config("x", {"a": "2"})  # 멱등 갱신
    assert c.get_config("x") == {"a": "2"}
    running, _ = c.is_running("x")
    assert running
    assert sink_kafka_offsets(c.offsets("x")) == {"t-0": 7}
    c.stop("x")  # 405 → pause 로 대체
    assert ("PUT", "/connectors/x/pause") in _FakeConnect.calls
    assert c.delete("x") is True
    assert c.delete("x") is False
    with pytest.raises(ConnectError):
        c.status("x")


def test_is_running_treats_status_404_as_not_yet(connect_server):
    """등록 직후 Connect 가 /status 에 404 (No status found) 를 돌려주는 동안은 예외가 아니라 False."""
    c = ConnectClient(connect_server, timeout=5)
    c.put_config("y", {"a": "1"})
    _FakeConnect.status_404_once.add("y")
    running, st = c.is_running("y")
    assert running is False
    assert st == {"pending": "status not available yet"}
    running, _ = c.is_running("y")  # 다음 조회부터 정상
    assert running is True


def test_connect_error_message_redacts_password(connect_server, env):
    c = ConnectClient(connect_server)
    try:
        c.status("cdc-secret-in-name")
    except ConnectError as exc:
        assert "cdc-secret" not in str(exc)
