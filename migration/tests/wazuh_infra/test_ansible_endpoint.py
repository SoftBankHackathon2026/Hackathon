"""Execute actual Ansible against an isolated HTTPS server, without cloud writes."""

import base64
import json
import os
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from migration.wazuh_infra.config import validate
from migration.wazuh_infra.runner import run


@pytest.fixture
def https_endpoint(remote_modules, tmp_path):

    _, _, material, _ = remote_modules
    ca_key, ca, _, ca_pem = material.certificate("ca")
    _, _, key, cert = material.certificate("endpoint", ca_key, ca, ("127.0.0.1",))
    (tmp_path / "ca.pem").write_bytes(ca_pem)
    (tmp_path / "server.pem").write_bytes(cert)
    (tmp_path / "server.key").write_bytes(key)
    state = {"redirect": False, "paths": [], "password": "unprinted-password-1234"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["paths"].append(self.path)
            expected = (
                "Basic "
                + base64.b64encode(("reader:" + state["password"]).encode()).decode()
            )
            if state["redirect"]:
                self.send_response(307)
                self.send_header("Location", "/unexpected-destination")
                self.end_headers()
                return
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":{"overall":{"level":"available"}}}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(tmp_path / "server.pem"), str(tmp_path / "server.key"))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    password = tmp_path / "password"
    password.write_text(state["password"])
    password.chmod(0o600)
    c = {
        "schema_version": 1,
        "request_id": "https-inspection",
        "endpoint": {
            "url": f"https://127.0.0.1:{server.server_port}",
            "ca_file": str(tmp_path / "ca.pem"),
            "username": "reader",
            "password_ref": str(password),
        },
    }
    try:
        yield state, validate(c, "inspect")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_ansible_tls_and_auth_result_is_secret_free(https_endpoint, monkeypatch):
    state, c = https_endpoint
    monkeypatch.setenv(
        "PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    )
    result = run(c, "inspect")
    assert result["status"] == "complete", json.dumps(result, indent=2)
    assert result["reports"][0]["http_status"] == 200
    assert result["reports"][0]["product_confirmed"] is False
    assert state["password"] not in json.dumps(result)
    assert result["full_migration_verified"] is False


def test_real_ansible_does_not_forward_auth_to_redirect(https_endpoint, monkeypatch):
    state, c = https_endpoint
    state["redirect"] = True
    monkeypatch.setenv(
        "PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    )
    result = run(c, "inspect")
    assert result["status"] == "partial", result
    assert state["paths"] == ["/api/status"]


def test_real_ansible_rejects_untrusted_ca(
    https_endpoint, remote_modules, tmp_path, monkeypatch
):
    state, c = https_endpoint
    _, _, material, _ = remote_modules
    _, _, _, ca = material.certificate("unrelated-ca")
    wrong = tmp_path / "wrong-ca.pem"
    wrong.write_bytes(ca)
    c["endpoint"]["ca_file"] = str(wrong)
    monkeypatch.setenv(
        "PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]
    )
    result = run(c, "inspect")
    assert result["status"] == "blocked", result
    assert "TLS_OR_CONNECTION_FAILED" in result["reason_codes"]
    assert state["paths"] == []
