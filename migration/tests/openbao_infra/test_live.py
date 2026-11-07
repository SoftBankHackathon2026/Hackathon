"""Real local TLS/static-seal server; never uses dev mode or production endpoints."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from migration.openbao_infra.config import validate
from migration.openbao_infra.runner import run

from .conftest import common, manage, material


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    binary = os.environ.get("OPENBAO_TEST_BINARY")
    if not binary:
        pytest.skip("OPENBAO_TEST_BINARY is required for real product tests")
    port, raft_port = free_port(), free_port()
    while port == raft_port:
        raft_port = free_port()
    cert, key = material.create_certificate(None, "127.0.0.1", 1)
    for name, data in (
        ("server.crt", cert),
        ("server.key", key),
        ("static.key", os.urandom(32)),
    ):
        (tmp_path / name).write_bytes(data)
        (tmp_path / name).chmod(0o600)
    (tmp_path / "data").mkdir()
    (tmp_path / "state").mkdir(mode=0o700)
    config_path = tmp_path / "server.hcl"
    config_path.write_text(f'''disable_mlock = true
api_addr = "https://127.0.0.1:{port}"
cluster_addr = "https://127.0.0.1:{raft_port}"
listener "tcp" {{
 address = "127.0.0.1:{port}"
 cluster_address = "127.0.0.1:{raft_port}"
 tls_cert_file = "{tmp_path}/server.crt"
 tls_key_file = "{tmp_path}/server.key"
}}
storage "raft" {{
 path = "{tmp_path}/data"
 node_id = "test"
}}
seal "static" {{
 current_key_id = "test"
 current_key = "file://{tmp_path}/static.key"
}}
audit "file" "test" {{
 options {{
  file_path = "{tmp_path}/audit.json"
 }}
}}
''')
    api = common.Client(f"https://127.0.0.1:{port}", str(tmp_path / "server.crt"))
    processes = []
    logs = []

    def start():
        log = (tmp_path / f"server-{len(logs)}.log").open("wb")
        logs.append(log)
        env = {
            k: v for k, v in os.environ.items() if not k.startswith(("BAO_", "VAULT_"))
        }
        p = subprocess.Popen(
            [binary, "server", "-config=" + str(config_path)],
            stdout=log,
            stderr=log,
            env=env,
        )
        processes.append(p)
        for _ in range(100):
            if p.poll() is not None:
                pytest.fail("OpenBao failed to start; inspect private local test log")
            try:
                api.health()
                return p
            except common.BaoError:
                time.sleep(0.1)
        pytest.fail("Local OpenBao startup timed out")

    p = start()
    try:
        yield tmp_path, api, p, start
    finally:
        for p in processes:
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=10)
        for log in logs:
            log.close()


@pytest.mark.openbao_live
def test_static_bootstrap_policy_boundaries_and_restart(
    server, raw_config, monkeypatch
):
    path, api, proc, start = server
    c = {
        "target": {"installation_id": "test", "mode": "reuse"},
        "endpoint": {"url": api.endpoint, "product_confirmed": True},
        "remote_ca": str(path / "server.crt"),
        "access": {
            "mount": "kv",
            "prefix": "migration",
            "token_ttl_seconds": 120,
            "secret_id_ttl_seconds": 60,
        },
        "applications": [],
        "credentials": {},
    }
    manager = manage.Manager(c, path / "state", api)
    assert manager.bootstrap()[0]["sealed"] is False
    assert manager.bootstrap()[1] is False
    raw_config["endpoint"] = {"url": api.endpoint, "ca_file": str(path / "server.crt")}
    monkeypatch.setenv(
        "PATH",
        str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""),
    )
    inspected = run("inspect", validate(raw_config, "inspect"))
    assert inspected["status"] == "inspected", inspected
    assert inspected["reports"][-1]["product_confirmed"] is False
    manager.authenticate()
    report, changed = manager.configure()
    assert changed and report["status"] == "partially_ready"
    # Real API read defaults must not trigger repeated changes.
    assert manager.configure()[1] is False
    manager.verify_seed()
    proc.terminate()
    proc.wait(timeout=10)
    start()
    for _ in range(100):
        if not api.health().get("sealed"):
            break
        time.sleep(0.1)
    c["restart_performed"] = True
    # This fixture owns the new test server; allow its restart evidence to count.
    c["target"]["mode"] = "install"
    report, _ = manager.verify()
    assert report["checks"]["migration"] == "passed"
    assert report["checks"]["restart"] == "passed"
    assert report["status"] == "partially_ready"
    assert report["recovery_verified"] is False
    assert report["checks"]["operator"] == "not_run"
    # A caller cannot finalize by presenting the bootstrap token as an operator.
    common.save_json(path / "state/handoff-ack.json", {"test": True})
    c["credentials"]["operator_token"] = api.token
    with pytest.raises(common.BaoError, match="INDEPENDENT_OPERATOR_REQUIRED"):
        manager.finalize()
