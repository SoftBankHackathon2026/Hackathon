"""Validate environment bootstrap, preservation and rollback without touching live services."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

path = Path(__file__).parents[2] / "ops" / "activate_providers.py"
spec = importlib.util.spec_from_file_location("activate_providers", path)
activation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activation)


@pytest.fixture
def inputs():
    manifest = json.loads((path.parents[1] / "deployment/provider-environments.json").read_text())
    bundle = {
        "database_url": "postgresql://demo:ephemeral@localhost/platform",
        "providers": [{"runtime": "aws_ecs", "config_file": "aws-runtime.json"}],
    }
    aws = {"env_file": "aws.env", "application": "demo", "allowed_images": []}
    return bundle, aws, manifest


def test_preserves_database_and_aws_settings_and_adds_real_default_image(inputs):
    bundle, aws, manifest = inputs
    saved = copy.deepcopy(inputs)
    result, new_aws, configs, dsn = activation.prepare(*inputs)
    assert inputs == saved
    assert result["database_url"] == bundle["database_url"] == dsn
    assert new_aws["env_file"] == aws["env_file"]
    assert new_aws["allowed_images"][0] == manifest["aws_demo_image"]
    assert len(result["providers"]) == 5
    legacy = next(p for p in result["providers"] if p["runtime"] == "onprem_argo")
    assert legacy["listed"] is False
    helm = [p for p in result["providers"] if p["runtime"] == "onprem_helm"]
    assert [p["config_file"] for p in helm if p.get("listed", True)] == ["helm-runtime.json"]
    assert (
        next(p for p in helm if p["config_file"] == "helm-canary-runtime.json")["listed"] is False
    )
    assert "database_url" not in configs["gcp"]
    assert configs["onprem"]["preview_probe_mode"] == "job"
    assert configs["onprem"]["kubeconfig"].endswith("/oneport-platform.kubeconfig")
    second = activation.prepare(result, new_aws, manifest)
    assert second == (result, new_aws, configs, dsn)


@pytest.mark.parametrize("provider", ["gcp", "onprem", "helm", "helm_canary"])
def test_manifest_cannot_replace_database_or_kubeconfig(inputs, provider):
    bundle, aws, manifest = inputs
    manifest[provider]["database_url"] = "postgresql://attacker/db"
    with pytest.raises(ValueError):
        activation.prepare(bundle, aws, manifest)


def test_existing_custom_provider_is_not_silently_removed(inputs):
    bundle, aws, manifest = inputs
    bundle["providers"].append({"runtime": "gcp_cloud_run", "config_file": "another-team.json"})
    with pytest.raises(ValueError):
        activation.prepare(bundle, aws, manifest)


def test_chart_upgrade_retains_verified_predecessor_and_rejects_identity_change(inputs, store):
    _, _, configs, _ = activation.prepare(*inputs)
    dsn = store.dsn
    new = copy.deepcopy(configs["helm"])
    packages = path.parents[1] / "vendor/helm"
    new["app_chart"]["path"] = str(packages / "oneport-app-0.2.0.tgz")
    old = copy.deepcopy(new)
    old.pop("observability", None)
    archive = packages / "oneport-app-0.1.1.tgz"
    old["app_chart"] = {
        "name": "oneport-app",
        "version": "0.1.1",
        "path": str(archive),
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    upgraded, changed = activation.reviewed_helm_upgrade(new, old, dsn)
    assert changed
    assert upgraded["previous_bindings"][0]["app_chart"] == old["app_chart"]
    again, changed = activation.reviewed_helm_upgrade(new, upgraded, dsn)
    assert not changed and again == upgraded
    foreign = copy.deepcopy(new)
    foreign["environment"]["application"] = "foreign-app"
    with pytest.raises(ValueError, match="explicit migration"):
        activation.reviewed_helm_upgrade(foreign, old, dsn)


@pytest.fixture
def gcp_client(inputs, monkeypatch):
    from google.cloud import run_v2
    from google.oauth2 import service_account

    config = inputs[2]["gcp"]
    env = config["environment"]
    name = f"projects/{env['project_id']}/locations/{env['region']}/services/{env['service']}"
    service = SimpleNamespace(
        name=name, uid=env["service_uid"], uri="https://oneport-demo-example.a.run.app"
    )
    client = Mock()
    client.get_service.return_value = service
    monkeypatch.setattr(run_v2, "ServicesClient", lambda **kwargs: client)
    monkeypatch.setattr(
        service_account.Credentials, "from_service_account_file", Mock(return_value=object())
    )
    return config, service, client


@pytest.mark.parametrize("custom_domain", [True, False])
def test_gcp_identity_is_independent_of_public_url(gcp_client, custom_domain):
    config, service, client = gcp_client
    if not custom_domain:
        config["environment"]["service_url"] = service.uri
    activation.preflight_gcp(config)
    client.get_service.assert_called_once_with(name=service.name, retry=None, timeout=10)
    client.transport.close.assert_called_once()


@pytest.mark.parametrize("field", ["name", "uid"])
def test_gcp_replaced_or_foreign_service_is_rejected(gcp_client, field):
    config, service, client = gcp_client
    setattr(service, field, "different-service")
    with pytest.raises(ValueError, match="service identity changed"):
        activation.preflight_gcp(config)
    client.transport.close.assert_called_once()


@pytest.fixture
def server(inputs, tmp_path, monkeypatch, store):
    bundle, aws, _ = inputs
    bundle = {**bundle, "database_url": store.dsn}
    (tmp_path / "secrets").mkdir()
    for name, data in [("platform.json", bundle), ("aws-runtime.json", aws)]:
        (tmp_path / name).write_text(json.dumps(data))
    monkeypatch.setattr(activation, "BASE", tmp_path)
    monkeypatch.setattr(activation, "SECRET_DIR", tmp_path / "secrets")

    def write_private(p, data):
        p.write_bytes(data)
        p.chmod(0o640)

    monkeypatch.setattr(activation, "atomic_write", write_private)
    monkeypatch.setattr(activation, "wait_for_api", lambda: None)
    monkeypatch.setattr(activation, "preflight_helm", lambda *args: None)
    monkeypatch.setattr(activation.time, "sleep", lambda n: None)
    monkeypatch.setattr(
        activation.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1)
    )
    return tmp_path


def test_preflight_failure_does_not_modify_config(server, monkeypatch):
    original = (server / "platform.json").read_bytes()

    def fail(*args):
        raise ValueError("invalid service binding")

    monkeypatch.setattr(activation, "preflight", fail)
    with pytest.raises(ValueError):
        activation.activate()
    assert (server / "platform.json").read_bytes() == original
    assert not (server / "gcp-runtime.json").exists()


def test_activation_publishes_custom_url_and_restarts_running_workers(server, monkeypatch, store):
    from pydantic import SecretStr

    from controlplane.execution.gcp.settings import GcpSettings
    from controlplane.providers.gcp.adapter import GcpAdapter

    monkeypatch.setattr(activation, "preflight", lambda *args: None)
    monkeypatch.setattr(
        activation.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0)
    )
    command = Mock()
    monkeypatch.setattr(activation, "command", command)
    activation.activate()
    config = json.loads((server / "gcp-runtime.json").read_text())
    settings = GcpSettings.model_validate(config)
    settings.database_url = SecretStr(store.dsn)
    assert (
        GcpAdapter(settings, store=store).public_environment()["service_url"]
        == "https://oneport-demo.gcp.yubin.dev"
    )
    command.assert_any_call("systemctl", "restart", *activation.SERVICES[2:])


@pytest.mark.parametrize("failure", ["restart", "is-active"])
def test_failed_worker_start_restores_previous_configuration(server, monkeypatch, failure):
    original = {p: p.read_bytes() for p in server.iterdir() if p.is_file()}
    monkeypatch.setattr(activation, "preflight", lambda *a: None)
    calls = []

    def command(*args):
        calls.append(args)
        if args[1] == failure and (failure != "restart" or "oneport-gcp-worker" in args):
            raise RuntimeError("worker failed")

    monkeypatch.setattr(activation, "command", command)
    with pytest.raises(RuntimeError):
        activation.activate()
    assert all(p.read_bytes() == data for p, data in original.items())
    assert not (server / "gcp-runtime.json").exists()
    assert not (server / "onprem-runtime.json").exists()
    assert not (server / "secrets/database-url").exists()
    assert ("systemctl", "disable", "oneport-gcp-worker") in calls


@pytest.mark.parametrize("fail_restart", [False, True])
def test_activation_holds_intake_and_worker_barrier_through_restart_and_restore(
    server, monkeypatch, store, inputs, fail_restart
):
    from controlplane.core.errors import RequestConflict
    from controlplane.domain.models import Runtime
    from controlplane.providers.onprem.helm.settings import HelmSettings

    _, _, configs, _ = activation.prepare(*inputs)
    raw = dict(configs["helm"])
    raw.pop("database_url_file")
    settings = HelmSettings.model_validate(raw | {"database_url": store.dsn})
    request = settings.demo_request()
    phases = []
    failed = False

    def guarded(phase):
        phases.append(phase)
        with pytest.raises(RequestConflict, match="설정을 갱신"):
            store.enqueue(
                request,
                "activation-window-request",
                settings.fingerprint(),
                runtime=Runtime.ONPREM_HELM,
            )
        with store.worker_lock() as conn:
            assert conn is None

    write = activation.atomic_write

    def guarded_write(path, data):
        guarded("write")
        write(path, data)

    def command(*args):
        nonlocal failed
        guarded(args[1])
        if fail_restart and not failed and args[1] == "restart" and "oneport-gcp-worker" in args:
            failed = True
            raise RuntimeError("worker restart failed")

    monkeypatch.setattr(activation, "preflight", lambda *args: guarded("preflight"))
    monkeypatch.setattr(activation, "atomic_write", guarded_write)
    monkeypatch.setattr(activation, "command", command)
    if fail_restart:
        with pytest.raises(RuntimeError, match="worker restart failed"):
            activation.activate()
    else:
        activation.activate()
    assert "write" in phases and "restart" in phases
    assert store.list() == []
    assert (
        store.enqueue(
            request, "after-activation-request", settings.fingerprint(), runtime=Runtime.ONPREM_HELM
        )["state"]
        == "queued"
    )
    with store.worker_lock() as conn:
        assert conn is not None


def test_changed_binding_stops_legacy_processes_before_checking_idle(server, monkeypatch, inputs):
    _, _, configs, _ = activation.prepare(*inputs)
    (server / "helm-runtime.json").write_text(json.dumps(configs["helm"]))
    monkeypatch.setattr(activation, "reviewed_helm_upgrade", lambda new, old, dsn: (new, True))
    monkeypatch.setattr(activation, "preflight", lambda *args: None)
    monkeypatch.setattr(
        activation.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0)
    )
    calls = []
    monkeypatch.setattr(activation, "command", lambda *args: calls.append(args))

    def idle(*args):
        assert (
            "systemctl",
            "stop",
            "oneport-api",
            "oneport-helm-worker",
            "oneport-helm-canary-worker",
        ) in calls
        assert not any(c[0:2] == ("systemctl", "restart") for c in calls)
        calls.append(("idle",))

    monkeypatch.setattr(activation, "require_idle_helm", idle)
    activation.activate()
    assert calls.index(("idle",)) < calls.index(
        ("systemctl", "restart", "oneport-api", "oneport-aws-worker")
    )
