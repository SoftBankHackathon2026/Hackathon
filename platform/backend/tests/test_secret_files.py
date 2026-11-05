import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_argo import harness as harness
from test_argo import sdk as sdk

from controlplane.core.config import operator_token_from_environment
from controlplane.core.secrets import DatabaseSettings, read_secret_file, secret_from_environment
from controlplane.execution.gcp.settings import GcpSettings
from controlplane.execution.onprem.settings import ArgoSettings
from controlplane.main import create_app
from controlplane.providers.gcp_bridge.models import GcpSettings as BridgeSettings
from controlplane.providers.loader import load_platform


def secret(tmp_path, value="postgresql://demo:private-password@db/platform", mode=0o600):
    path = tmp_path / "secret"
    path.write_text(value)
    path.chmod(mode)
    return path


@pytest.mark.parametrize("mode", [0o400, 0o600, 0o440, 0o640])
def test_private_files_and_projected_symlinks(tmp_path, mode):
    target = secret(tmp_path, "test-secret\n", mode)
    link = tmp_path / "mounted"
    link.symlink_to(target)
    assert read_secret_file(link).get_secret_value() == "test-secret"
    assert "test-secret" not in repr(read_secret_file(link))


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o666])
def test_unsafe_file_permissions_rejected(tmp_path, mode):
    with pytest.raises(ValueError, match="Invalid secret file"):
        read_secret_file(secret(tmp_path, mode=mode))


@pytest.mark.parametrize("value", ["", " \n", "a\nb", "a\x00b", "a" * 65537])
def test_invalid_content_never_echoed(tmp_path, value):
    with pytest.raises(ValueError, match="Invalid secret file") as exc:
        read_secret_file(secret(tmp_path, value))
    assert "private-password" not in str(exc.value)


def test_missing_relative_and_fifo_paths_fail(tmp_path):
    for path in (Path("relative-secret"), tmp_path / "missing"):
        with pytest.raises(ValueError, match="Invalid secret file"):
            read_secret_file(path)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError, match="Invalid secret file"):
        read_secret_file(fifo)


def test_env_file_and_direct_value_are_mutually_exclusive(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OPERATOR_FILE", str(secret(tmp_path, "operator-secret")))
    monkeypatch.delenv("TEST_OPERATOR", raising=False)
    assert secret_from_environment("TEST_OPERATOR").get_secret_value() == "operator-secret"
    monkeypatch.setenv("TEST_OPERATOR", "different-secret")
    with pytest.raises(ValueError, match="Set only TEST_OPERATOR"):
        secret_from_environment("TEST_OPERATOR")


def test_database_file_rotation_is_applied_on_reload(tmp_path):
    path = secret(tmp_path)
    original = DatabaseSettings(database_url_file=path)
    path.write_text("postgresql://new-password@db/platform")
    reloaded = DatabaseSettings(database_url_file=path)
    assert original.database_url != reloaded.database_url
    assert "database_url_file" not in reloaded.model_dump()
    assert "new-password" not in reloaded.model_dump_json()
    with pytest.raises(ValueError, match="Set only database_url"):
        DatabaseSettings(database_url="inline", database_url_file=path)


@pytest.mark.parametrize(
    "model,example",
    [(ArgoSettings, "onprem"), (GcpSettings, "gcp"), (BridgeSettings, "gcp-bridge")],
)
def test_provider_fingerprints_do_not_change_when_moving_secret_to_file(tmp_path, model, example):
    path = Path(__file__).resolve().parents[2] / "examples" / example / "runtime.json"
    raw = json.loads(path.read_text())
    inline = model.model_validate(raw)
    raw["database_url_file"] = str(secret(tmp_path, raw.pop("database_url")))
    mounted = model.model_validate_json(json.dumps(raw))
    assert inline.database_url == mounted.database_url
    assert inline.fingerprint() == mounted.fingerprint()


def test_api_bundle_and_worker_share_mounted_database_secret(harness, tmp_path, monkeypatch):
    h = harness
    database = secret(tmp_path, h.store.dsn)
    token_file = tmp_path / "operator"
    token = "operator-file-test-token-01234567890123456789"
    token_file.write_text(token)
    token_file.chmod(0o600)
    runtime = h.runtime.settings.model_dump(mode="json", exclude={"database_url"})
    runtime["database_url_file"] = str(database)
    (tmp_path / "runtime.json").write_text(json.dumps(runtime))
    bundle = {
        "database_url_file": str(database),
        "providers": [{"runtime": "onprem_argo", "config_file": "runtime.json"}],
    }
    config = tmp_path / "platform.json"
    config.write_text(json.dumps(bundle))
    monkeypatch.delenv("ONEPORT_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("ONEPORT_RUNTIME_CONFIG", raising=False)
    monkeypatch.setenv("ONEPORT_OPERATOR_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("ONEPORT_CONFIG", str(config))
    loaded = load_platform()
    registry, store = loaded.registry, loaded.store
    resolved_token = operator_token_from_environment().get_secret_value()
    assert store.dsn == h.store.dsn and resolved_token == token
    with TestClient(
        create_app(registry=registry, store=store, operator_token=resolved_token)
    ) as client:
        assert client.get("/api/v1/releases").status_code == 401
        response = client.get("/api/v1/releases", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert token not in response.text and h.store.dsn not in response.text
