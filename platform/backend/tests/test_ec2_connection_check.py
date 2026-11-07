import hashlib
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "ec2" / "check-connection.sh"
BOOTSTRAP = b"#!/bin/sh\necho bootstrap\n"
SHA = hashlib.sha384(BOOTSTRAP).hexdigest()


class Platform:
    def __init__(self):
        self.capabilities = {
            "available": True,
            "reason": None,
            "releaseVersion": "conn-1",
            "bootstrapUrl": "https://platform.example/releases/conn-1/bootstrap.sh",
            "providers": ["kubernetes"],
        }
        self.capabilities_status = 200
        self.bootstrap = BOOTSTRAP
        self.bootstrap_status = 200


@pytest.fixture
def platform():
    state = Platform()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/v1/connections/capabilities":
                body, status = json.dumps(state.capabilities).encode(), state.capabilities_status
            elif self.path == "/releases/conn-1/bootstrap.sh":
                body, status = state.bootstrap, state.bootstrap_status
            else:
                body, status = b"{}", 404
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture
def root(tmp_path):
    (tmp_path / "connection" / "broker").mkdir(parents=True)
    write_config(tmp_path)
    return tmp_path


def write_config(
    root, *, env_version="conn-1", env_sha=SHA, broker_version="conn-1", broker_sha=SHA
):
    (root / "connection" / "server.env").write_text(
        f"ONEPORT_CONNECTION_ENABLED=true\n"
        f"ONEPORT_CONNECTION_RELEASE_VERSION={env_version}\n"
        f"ONEPORT_CONNECTION_BOOTSTRAP_SHA384={env_sha}\n"
    )
    (root / "connection" / "broker" / "broker.json").write_text(
        json.dumps({"release_version": broker_version, "bootstrap_sha384": broker_sha})
    )


def check(root, base, wait=0):
    return subprocess.run(
        ["bash", str(SCRIPT), str(root), "platform.example", str(wait), base],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_passes_when_everything_matches(root, platform):
    result = check(root, platform.base)
    assert result.returncode == 0, result.stderr


def test_reports_the_reason_when_the_feature_is_off(root, platform):
    platform.capabilities |= {
        "available": False,
        "reason": "보안 연결 실행 환경이 아직 준비되지 않았습니다.",
    }
    result = check(root, platform.base)
    assert result.returncode == 1
    assert "연결 기능이 꺼져 있습니다" in result.stderr
    assert "아직 준비되지 않았습니다" in result.stderr


def test_fails_when_the_api_announces_another_version(root, platform):
    platform.capabilities["releaseVersion"] = "conn-0"
    result = check(root, platform.base)
    assert result.returncode == 1
    assert "conn-0" in result.stderr and "conn-1" in result.stderr


@pytest.mark.parametrize(
    "override",
    [{"broker_version": "conn-2"}, {"broker_sha": "0" * 96}],
)
def test_fails_when_broker_and_api_settings_differ(root, platform, override):
    write_config(root, **override)
    result = check(root, platform.base)
    assert result.returncode == 1
    assert "broker.json" in result.stderr


def test_fails_when_the_served_bootstrap_hash_differs(root, platform):
    platform.bootstrap = BOOTSTRAP + b"# changed\n"
    result = check(root, platform.base)
    assert result.returncode == 1
    assert "해시가 설정과 다릅니다" in result.stderr


def test_fails_when_the_bootstrap_cannot_be_downloaded(root, platform):
    platform.bootstrap_status = 404
    result = check(root, platform.base)
    assert result.returncode == 1
    assert "bootstrap.sh 를 받지 못했습니다" in result.stderr


def test_fails_when_the_api_does_not_answer(root, platform):
    platform.capabilities_status = 503
    result = check(root, platform.base)
    assert result.returncode == 1
    assert "조회에 실패" in result.stderr


def test_fails_when_nothing_listens(root):
    result = check(root, "http://127.0.0.1:9")
    assert result.returncode == 1
    assert "조회에 실패" in result.stderr


@pytest.mark.parametrize("missing", ["connection/server.env", "connection/broker/broker.json"])
def test_fails_when_a_settings_file_is_missing(root, platform, missing):
    (root / missing).unlink()
    result = check(root, platform.base)
    assert result.returncode == 1
    assert missing in result.stderr


def test_fails_when_server_env_has_no_version(root, platform):
    write_config(root, env_version="", broker_version="")
    result = check(root, platform.base)
    assert result.returncode == 1
    assert "버전·해시가 없습니다" in result.stderr


def test_waits_until_the_feature_comes_up(root, platform):
    platform.capabilities["available"] = False
    threading.Timer(1.0, lambda: platform.capabilities.update(available=True)).start()
    started = time.monotonic()
    result = check(root, platform.base, wait=20)
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 15


def test_does_not_wait_when_wait_is_zero(root, platform):
    platform.capabilities["available"] = False
    started = time.monotonic()
    result = check(root, platform.base, wait=0)
    assert result.returncode == 1
    assert time.monotonic() - started < 3


def test_gives_up_after_the_wait(root, platform):
    platform.capabilities["available"] = False
    started = time.monotonic()
    result = check(root, platform.base, wait=4)
    assert result.returncode == 1
    assert 3 <= time.monotonic() - started < 15
