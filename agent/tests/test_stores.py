from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from agent.stores import AwsSecretsManagerStore, FileStore, StoreError, VaultKvStore, VersionConflict, build_store
from agent.config import StoreConfig


def test_file_store_roundtrip_and_mask(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"DB_HOST": "a", "DB_PASSWORD": "pw"}), encoding="utf-8")
    store = FileStore(p)
    snap = store.read()
    assert snap.values["DB_HOST"] == "a"
    assert snap.masked(("DB_PASSWORD",)) == {"DB_HOST": "a", "DB_PASSWORD": "***"}
    new = store.write({"DB_HOST": "b", "DB_PASSWORD": "pw"}, expected_version=snap.version)
    assert new.version != snap.version
    assert json.loads(p.read_text())["DB_HOST"] == "b"


def test_file_store_version_conflict(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"DB_HOST": "a"}), encoding="utf-8")
    store = FileStore(p)
    snap = store.read()
    p.write_text(json.dumps({"DB_HOST": "changed-elsewhere"}), encoding="utf-8")
    with pytest.raises(VersionConflict):
        store.write({"DB_HOST": "b"}, expected_version=snap.version)


def test_file_store_errors(tmp_path):
    with pytest.raises(StoreError):
        FileStore(tmp_path / "missing.json").read()
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(StoreError):
        FileStore(bad).read()


# ---- Vault KV v2 (가짜 서버) -------------------------------------------------


class FakeVault(BaseHTTPRequestHandler):
    state: dict[str, Any] = {"version": 3, "data": {"DB_HOST": "wrong", "DB_PASSWORD": "pw"}}
    seen: list[dict[str, Any]] = []

    def log_message(self, *a):  # noqa: D102
        pass

    def _send(self, code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        if self.headers.get("X-Vault-Token") != "t0k":
            return self._send(403, {"errors": ["permission denied"]})
        assert self.path == "/v1/secret/data/hackathon/app"
        s = type(self).state
        self._send(200, {"data": {"data": s["data"], "metadata": {"version": s["version"]}}})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append(body)
        s = type(self).state
        if body.get("options", {}).get("cas") != s["version"]:
            return self._send(400, {"errors": ["check-and-set parameter did not match the current version"]})
        s["data"] = body["data"]
        s["version"] += 1
        self._send(200, {"data": {"version": s["version"]}})


@pytest.fixture
def vault_url():
    FakeVault.state = {"version": 3, "data": {"DB_HOST": "wrong", "DB_PASSWORD": "pw"}}
    FakeVault.seen = []
    srv = HTTPServer(("127.0.0.1", 0), FakeVault)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_vault_read_write_cas(vault_url):
    store = VaultKvStore(vault_url, "secret", "hackathon/app", token="t0k")
    snap = store.read()
    assert snap.version == "3" and snap.values["DB_HOST"] == "wrong"
    new = store.write({**snap.values, "DB_HOST": "right"}, expected_version=snap.version)
    assert new.version == "4"
    assert FakeVault.seen[-1]["options"] == {"cas": 3}
    with pytest.raises(VersionConflict):
        store.write({"DB_HOST": "again"}, expected_version="3")


def test_vault_bad_token(vault_url):
    with pytest.raises(StoreError, match="403"):
        VaultKvStore(vault_url, "secret", "hackathon/app", token="bad").read()


def test_vault_requires_token(monkeypatch):
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    with pytest.raises(StoreError, match="VAULT_TOKEN"):
        VaultKvStore("http://x", "secret", "p")


# ---- AWS Secrets Manager (가짜 클라이언트) ------------------------------------


class FakeSm:
    def __init__(self):
        self.version = "v1"
        self.text = json.dumps({"DB_HOST": "wrong", "DB_PASSWORD": "pw"})
        self.puts: list[dict[str, Any]] = []

    def get_secret_value(self, SecretId):  # noqa: N803
        assert SecretId == "hackathon/sample-app/aws"
        return {"SecretString": self.text, "VersionId": self.version}

    def put_secret_value(self, SecretId, SecretString, ClientRequestToken):  # noqa: N803
        self.puts.append({"SecretId": SecretId, "SecretString": SecretString, "token": ClientRequestToken})
        self.text, self.version = SecretString, "v2"
        return {"VersionId": "v2"}


def test_aws_store_roundtrip():
    client = FakeSm()
    store = AwsSecretsManagerStore("hackathon/sample-app/aws", client=client)
    snap = store.read()
    assert snap.version == "v1"
    new = store.write({**snap.values, "DB_HOST": "right"}, expected_version="v1")
    assert new.version == "v2" and json.loads(client.puts[0]["SecretString"])["DB_HOST"] == "right"
    with pytest.raises(VersionConflict):
        store.write({"DB_HOST": "x"}, expected_version="v1")


def test_aws_store_requires_json_object():
    client = FakeSm()
    client.text = "plain"
    with pytest.raises(StoreError):
        AwsSecretsManagerStore("hackathon/sample-app/aws", client=client).read()


def test_build_store_file(tmp_path):
    assert isinstance(build_store(StoreConfig("file", path=str(tmp_path / "x.json"))), FileStore)
