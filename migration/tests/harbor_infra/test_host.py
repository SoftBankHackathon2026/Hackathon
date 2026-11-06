import importlib.util
import io
import sys
import tarfile
from pathlib import Path

import pytest
import yaml
from migration.harbor_infra.ansible.module_utils import harbor_core
from migration.harbor_infra.config import ASSETS

sys.modules["ansible.module_utils.harbor_core"] = harbor_core
spec = importlib.util.spec_from_file_location(
    "test_harbor_host", ASSETS / "module_utils/harbor_host.py"
)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


@pytest.mark.parametrize(
    "entry", ["../escape", "/absolute/escape", "harbor/../../escape", "another/file"]
)
def test_archive_traversal_rejected(tmp_path, entry):
    archive = tmp_path / "archive.tgz"
    with tarfile.open(archive, "w:gz") as t:
        item = tarfile.TarInfo(entry)
        item.size = 1
        t.addfile(item, io.BytesIO(b"x"))
    with pytest.raises(harbor_core.HarborError):
        host.safe_extract(archive, tmp_path / "target")


def test_archive_symlinks_rejected(tmp_path):
    archive = tmp_path / "archive.tgz"
    with tarfile.open(archive, "w:gz") as t:
        item = tarfile.TarInfo("harbor/link")
        item.type = tarfile.SYMTYPE
        item.linkname = "/etc/passwd"
        t.addfile(item)
    with pytest.raises(harbor_core.HarborError):
        host.safe_extract(archive, tmp_path / "target")


def test_real_openssl_certificates_survive_rerun(tmp_path):
    c = {"tls": {"mode": "self_signed"}, "endpoint": {"url": "https://127.0.0.1:8444"}}
    result = host.prepare_certificates(c, {}, tmp_path / "certificates")
    key = tmp_path / "certificates/server.key"
    before = key.read_bytes()
    assert host.prepare_certificates(c, {}, key.parent) == result
    assert key.read_bytes() == before and key.stat().st_mode & 0o777 == 0o600
    assert "PRIVATE KEY" not in result


@pytest.fixture
def k3s(tmp_path, monkeypatch):
    original = Path

    def mapped(value):
        text = str(value)
        return (
            tmp_path / text.lstrip("/")
            if text.startswith(("/etc/", "/var/lib/"))
            else original(value)
        )

    monkeypatch.setattr(host, "Path", mapped)
    monkeypatch.setattr(host, "identity", lambda *args: None)
    monkeypatch.setattr(host.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        host.shutil, "which", lambda name: "/usr/bin/k3s" if name == "k3s" else None
    )
    config = mapped("/etc/rancher/k3s/registries.yaml")
    config.parent.mkdir(parents=True)
    config.write_text(
        yaml.safe_dump(
            {
                "mirrors": {"ghcr.io": {"endpoint": ["https://existing.example"]}},
                "configs": {
                    "existing.example": {
                        "auth": {"username": "old", "password": "PRIVATE-EXISTING"}
                    }
                },
            }
        )
    )
    commands = []
    monkeypatch.setattr(
        host, "command", lambda argv, **kwargs: commands.append(argv) or ("", 0)
    )
    c = {
        "organization_id": "org",
        "endpoint": {"url": "https://registry.example:8444"},
        "trust": {"targets": ["node-1"], "restart_approved": []},
    }
    node = {
        "name": "node-1",
        "uid": "uid",
        "machine_id": "machine",
        "container_runtime_version": "containerd://2.0.0-k3s1",
    }
    return c, node, config, commands, tmp_path


def test_k3s_preserves_other_registries_and_defers_restart(k3s):
    c, node, file, commands, _ = k3s
    result = host.trust(c, node, "-----BEGIN CERTIFICATE-----\nPUBLIC\n", True)
    assert result["restart_required"] and result["status"] == "partially_ready"
    assert not commands
    after = yaml.safe_load(file.read_text())
    assert (
        after["configs"]["existing.example"]["auth"]["password"] == "PRIVATE-EXISTING"
    )
    assert (
        "registry.example:8444" in after["configs"]
        and "registry.example:8444" not in after["mirrors"]
    )
    assert "auth" not in after["configs"]["registry.example:8444"]
    assert not after["configs"]["registry.example:8444"]["tls"].get(
        "insecure_skip_verify"
    )


def test_k3s_restart_requires_target_approval_and_repeat_is_stable(k3s):
    c, node, file, commands, _ = k3s
    ca = "-----BEGIN CERTIFICATE-----\nPUBLIC\n"
    host.trust(c, node, ca, True)
    c["trust"]["restart_approved"] = ["node-1"]
    result = host.trust(c, node, ca, True)
    assert result["trust_activated"]
    assert ["systemctl", "restart", "k3s"] in commands
    before = len(commands)
    host.trust(c, node, ca, True)
    assert len(commands) == before


def test_k3s_external_edit_is_not_overwritten(k3s):
    c, node, file, _, _ = k3s
    ca = "-----BEGIN CERTIFICATE-----\nPUBLIC\n"
    host.trust(c, node, ca, True)
    file.write_text(file.read_text() + "\n# external change\n")
    with pytest.raises(
        harbor_core.HarborError, match="TRUST_SETTINGS_MODIFIED_EXTERNALLY"
    ):
        host.trust(c, node, ca, True)


def test_existing_harbor_trust_is_not_adopted_silently(k3s):
    c, node, file, _, _ = k3s
    file.write_text(
        yaml.safe_dump(
            {
                "configs": {
                    "registry.example:8444": {"tls": {"ca_file": "/someone/else"}}
                }
            }
        )
    )
    with pytest.raises(
        harbor_core.HarborError, match="EXISTING_TRUST_SETTINGS_CONFLICT"
    ):
        host.trust(c, node, "-----BEGIN CERTIFICATE-----\nPUBLIC\n", True)


@pytest.mark.parametrize(
    "available,passes", [(2_000_000_000, False), (20_000_000_000, True)]
)
def test_docker_disk_budget_is_separate_from_image_data(
    tmp_path, monkeypatch, available, passes
):
    from types import SimpleNamespace

    monkeypatch.setattr(
        host.shutil, "disk_usage", lambda path: SimpleNamespace(free=available)
    )
    if passes:
        host.check_docker_space(tmp_path)
    else:
        with pytest.raises(harbor_core.HarborError, match="INSUFFICIENT_DOCKER_DISK"):
            host.check_docker_space(tmp_path)
