"""OpenBaoKv 를 KV v2 를 흉내 낸 로컬 HTTP 서버로 시험한다: 읽기, 합쳐 쓰기, cas 충돌, 봉인."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent.kv import CasConflict, KvError, OpenBaoKv

TOKEN = "s.test-token-value"


class Bao:
    def __init__(self) -> None:
        self.data = {"DB_HOST": "wrong", "DB_PORT": "5432", "DB_NAME": "n", "DB_USER": "u"}
        self.version, self.sealed, self.posts, self.tokens = 3, False, [], []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code: int, doc: dict) -> None:
                raw = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                outer.tokens.append(self.headers.get("X-Vault-Token"))
                if outer.sealed:
                    return self._send(503, {"errors": ["Vault is sealed"]})
                self._send(200, {"data": {"data": outer.data, "metadata": {"version": outer.version}}})

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.posts.append(body)
                if body["options"]["cas"] != outer.version:
                    return self._send(400, {"errors": ["check-and-set parameter did not match the current version"]})
                outer.data, outer.version = body["data"], outer.version + 1
                self._send(200, {"data": {"version": outer.version}})

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addr = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def bao():
    server = Bao()
    yield server
    server.server.shutdown()
    server.server.server_close()


def kv(bao: Bao) -> OpenBaoKv:
    return OpenBaoKv(bao.addr, "secret", "hackathon/sample-app/onprem", TOKEN, timeout=2)


def test_read_returns_data_and_version(bao: Bao) -> None:
    snap = kv(bao).read()
    assert snap.version == 3 and snap.data["DB_HOST"] == "wrong"
    assert bao.tokens == [TOKEN]


def test_write_merges_and_uses_cas(bao: Bao) -> None:
    assert kv(bao).write_changes({"DB_HOST": "good"}, expected_version=3) == 4
    assert bao.posts[0]["options"] == {"cas": 3}
    assert bao.data == {"DB_HOST": "good", "DB_PORT": "5432", "DB_NAME": "n", "DB_USER": "u"}


def test_version_moved_before_write_conflicts_without_post(bao: Bao) -> None:
    with pytest.raises(CasConflict):
        kv(bao).write_changes({"DB_HOST": "good"}, expected_version=2)
    assert bao.posts == []


def test_server_side_cas_rejection_is_conflict(bao: Bao) -> None:
    """읽은 직후 다른 쓰기가 끼어든 경우. 서버의 400 check-and-set 도 충돌이다."""
    store = kv(bao)
    real_read = store.read

    def racing_read():
        snap = real_read()
        bao.version += 1
        return snap

    store.read = racing_read  # type: ignore[method-assign]
    with pytest.raises(CasConflict):
        store.write_changes({"DB_HOST": "good"}, expected_version=3)
    assert bao.data["DB_HOST"] == "wrong"


def test_sealed_is_kv_error_without_token(bao: Bao) -> None:
    bao.sealed = True
    with pytest.raises(KvError) as err:
        kv(bao).read()
    assert "503" in str(err.value) and TOKEN not in str(err.value)
    assert not isinstance(err.value, CasConflict)


def test_unreachable_is_kv_error() -> None:
    with pytest.raises(KvError):
        OpenBaoKv("http://127.0.0.1:9", "secret", "p", TOKEN, timeout=1).read()


def test_repr_hides_token(bao: Bao) -> None:
    assert TOKEN not in repr(kv(bao))
