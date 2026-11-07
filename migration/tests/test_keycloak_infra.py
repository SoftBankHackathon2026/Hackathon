"""Contract, discovery, wrong-target protection and process isolation regressions."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys

import pytest
from migration.keycloak_infra import config, discovery, runner
from migration.keycloak_infra.__main__ import main


@pytest.fixture
def raw(tmp_path):
    refs = {}
    for name in ("kubeconfig", "key", "known_hosts", "cert", "ca"):
        file = tmp_path / name
        file.write_text("fixture")
        refs[name] = str(file)
    return {
        "schema_version": 1,
        "request_id": "request-1",
        "cluster": {
            "id": "cluster-1",
            "kubeconfig": refs["kubeconfig"],
            "context": "onprem",
        },
        "nodes": [
            {
                "name": "node-a",
                "uid": "uid-a",
                "ssh_host": "192.0.2.10",
                "ssh_user": "operator",
                "private_key_file": refs["key"],
                "known_hosts_file": refs["known_hosts"],
            }
        ],
        "endpoint": {"base_url": "https://sso.example.test:8443"},
        "install": {
            "id": "installation-1",
            "node_name": "node-a",
            "base_url": "https://sso.example.test:8443",
            "certificate_file": refs["cert"],
            "private_key_file": refs["key"],
            "ca_file": refs["ca"],
            "db_password_env": "KC_TEST_DB_PASSWORD",
            "bootstrap_password_env": "KC_TEST_BOOTSTRAP_PASSWORD",
        },
    }


def common():
    path = config.ASSETS / "module_utils/keycloak_common.py"
    spec = importlib.util.spec_from_file_location("test_keycloak_common", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("operation", config.OPERATIONS)
def test_public_config_contains_no_secrets(raw, operation):
    c = config.validate_config(raw, operation, resolve_secrets=False)
    assert "fixture" not in json.dumps(c)
    assert "secrets" not in c or not any(c["secrets"].values())


def test_install_resolves_only_environment_references(raw, monkeypatch):
    monkeypatch.setenv("KC_TEST_DB_PASSWORD", "a" * 32)
    monkeypatch.setenv("KC_TEST_BOOTSTRAP_PASSWORD", "b" * 32)
    c = config.validate_config(raw, "install")
    assert c["secrets"]["db_password"] == "a" * 32
    assert "secrets" not in raw
    assert "secrets" not in config.validate_config(raw, "preflight")


@pytest.mark.parametrize(
    "value", ["", "short", "x" * 24 + "\n", "x" * 24 + '"', "x" * 24 + "$foo"]
)
def test_unsafe_secret_never_appears_in_error(raw, monkeypatch, value):
    monkeypatch.setenv("KC_TEST_DB_PASSWORD", value)
    with pytest.raises(config.ConfigError) as error:
        config.validate_config(raw, "install")
    if value:
        assert value not in str(error.value)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:secret@example.test",
        "https://example.test?password=secret",
        "https://example.test/#x",
        "https://example.test:99999",
        "file:///etc/passwd",
        "https://example.test\nxxx",
    ],
)
def test_endpoint_rejects_embedded_credentials_and_invalid_urls(raw, base_url):
    raw["endpoint"]["base_url"] = base_url
    with pytest.raises(config.ConfigError):
        config.validate_config(raw, "inspect")


def test_install_requires_selected_cluster_node(raw):
    raw["install"]["node_name"] = "outside-cluster"
    with pytest.raises(config.ConfigError):
        config.validate_config(raw, "preflight")


def test_duplicate_node_mapping_rejected(raw):
    raw["nodes"].append(copy.deepcopy(raw["nodes"][0]))
    with pytest.raises(config.ConfigError):
        config.validate_config(raw, "discover")


def test_install_listener_and_published_url_must_agree(raw):
    raw["install"]["base_url"] = "https://sso.example.test"
    with pytest.raises(config.ConfigError):
        config.validate_config(raw, "preflight")


def test_arbitrary_ansible_options_and_inline_secrets_rejected(raw):
    raw["nodes"][0]["ansible_connection"] = "local"
    with pytest.raises(config.ConfigError):
        config.validate_config(raw, "discover")


def test_inspect_does_not_require_unused_cluster_ssh_files(raw):
    raw["cluster"]["kubeconfig"] = "/not/available"
    raw["nodes"][0]["private_key_file"] = "/not/available"
    c = config.validate_config(raw, "inspect")
    assert c["nodes"] == []
    assert "cluster" not in c


def test_boolean_schema_version_is_invalid(raw):
    raw["schema_version"] = True
    with pytest.raises(config.ConfigError):
        config.validate_config(raw, "discover")


def test_invalid_selected_node_shape_is_config_error(raw):
    raw["install"]["node_name"] = {"unexpected": "object"}
    with pytest.raises(config.ConfigError):
        config.validate_config(raw, "preflight")


def test_operator_is_not_a_keycloak_server_candidate():
    objects = resources()
    objects[-1]["spec"]["containers"][0]["image"] = (
        "quay.io/keycloak/keycloak-operator:26.8.2"
    )
    report = discovery.cluster_report(
        [{"item": {"kind": "Pod"}, "resources": objects}], "cluster"
    )
    assert not report["candidates"]


def test_scaled_zero_workload_is_still_a_candidate():
    obj = {
        "kind": "Deployment",
        "metadata": {
            "uid": "deployment-zero",
            "name": "keycloak",
            "namespace": "identity",
        },
        "spec": {
            "replicas": 0,
            "template": {
                "spec": {"containers": [{"image": "quay.io/keycloak/keycloak:26.8.2"}]}
            },
        },
    }
    report = discovery.cluster_report(
        [{"item": {"kind": "Deployment"}, "resources": [obj]}], "cluster"
    )
    assert len(report["candidates"]) == 1
    assert not report["candidates"][0]["pods"]


def test_preflight_failed_check_returns_blocked(raw):
    c = config.validate_config(raw, "preflight")
    reports = {
        "cluster": {"nodes": []},
        "target": {"checks": [{"name": "managed_ownership", "status": "fail"}]},
    }
    result = discovery.aggregate("preflight", c, reports, [], 0)
    assert result["status"] == "blocked"
    assert "CHECK_MANAGED_OWNERSHIP" in result["reason_codes"]


def test_strict_host_keys_and_single_install_target(raw):
    c = config.validate_config(raw, "preflight")
    inv = runner.inventory(c, "preflight")
    nodes = inv["all"]["children"]["keycloak_nodes"]["hosts"]
    assert len(nodes) == 1
    assert "StrictHostKeyChecking=yes" in nodes["node_0"]["ansible_ssh_common_args"]
    assert "ansible_password" not in nodes["node_0"]


def resources():
    node = {
        "kind": "Node",
        "metadata": {"name": "node-a", "uid": "uid-a"},
        "status": {"nodeInfo": {"machineID": "machine-a"}},
    }
    deployment = {
        "kind": "Deployment",
        "metadata": {"name": "sso", "uid": "deploy-1", "namespace": "identity"},
    }
    rs = {
        "kind": "ReplicaSet",
        "metadata": {
            "uid": "rs-1",
            "ownerReferences": [{"uid": "deploy-1", "controller": True}],
        },
    }
    pod = {
        "kind": "Pod",
        "metadata": {
            "name": "sso-1",
            "uid": "pod-1",
            "namespace": "identity",
            "labels": {"app": "sso"},
            "ownerReferences": [{"uid": "rs-1", "controller": True}],
        },
        "spec": {
            "nodeName": "node-a",
            "containers": [
                {
                    "image": "quay.io/keycloak/keycloak:26.8.2",
                    "env": [{"name": "PASSWORD", "value": "do-not-leak"}],
                }
            ],
        },
    }
    return [node, deployment, rs, pod]


def test_discovery_groups_replicas_by_controller_and_redacts_secrets():
    objects = resources()
    second = copy.deepcopy(objects[-1])
    second["metadata"].update({"name": "sso-2", "uid": "pod-2"})
    second["spec"]["nodeName"] = "node-b"
    report = discovery.cluster_report(
        [{"item": {"kind": "Pod"}, "resources": [*objects, second]}], "cluster"
    )
    assert len(report["candidates"]) == 1
    assert report["candidates"][0]["nodes"] == ["node-a", "node-b"]
    assert "do-not-leak" not in json.dumps(report)
    assert "env" not in json.dumps(report)


def test_permission_failure_not_converted_to_absence():
    report = discovery.cluster_report(
        [{"item": {"kind": "Pod"}, "failed": True, "msg": "secret"}], "cluster"
    )
    assert report["coverage"][0]["status"] == "unknown"
    assert "secret" not in json.dumps(report)


def test_missing_ssh_mapping_is_partial(raw):
    c = config.validate_config(raw, "discover")
    c["nodes"] = []
    cluster = discovery.cluster_report(
        [{"item": {"kind": "Node"}, "resources": resources()}], "cluster"
    )
    result = discovery.aggregate("discover", c, {"cluster": cluster}, [], 0)
    assert result["status"] == "partial"
    assert "NODE_SSH_INFORMATION_MISSING_OR_STALE" in result["reason_codes"]


def test_missing_host_report_is_not_complete(raw):
    c = config.validate_config(raw, "discover")
    cluster = discovery.cluster_report(
        [{"item": {"kind": "Node"}, "resources": resources()}], "cluster"
    )
    result = discovery.aggregate("discover", c, {"cluster": cluster}, [], 0)
    assert result["status"] == "partial"


def test_empty_cluster_is_not_success(raw):
    c = config.validate_config(raw, "discover")
    result = discovery.aggregate("discover", c, {"cluster": {"nodes": []}}, [], 0)
    assert result["status"] == "partial"
    assert "NODE_DISCOVERY_EMPTY_OR_UNAVAILABLE" in result["reason_codes"]


def test_incomplete_install_never_reports_success(raw):
    c = config.validate_config(raw, "install", resolve_secrets=False)
    result = discovery.aggregate(
        "install", c, {"cluster": {"nodes": resources()[:1]}, "target": {}}, [], 0
    )
    assert result["status"] == "failed"
    assert "INSTALLATION_NOT_VERIFIED" in result["reason_codes"]


def test_machine_identity_rejects_replaced_or_cloned_node():
    probe = common()
    assert probe.same_machine({"machine_id": "ABC-123"}, {"machine_id": "abc123"})
    assert not probe.same_machine({}, {})
    assert not probe.same_machine(
        {"machine_id": "abc", "system_uuid": "123"},
        {"machine_id": "abc", "system_uuid": "456"},
    )


def test_native_discovery_handles_custom_unit_name_without_arguments():
    result = common().unit_candidates(
        "Id=custom-sso.service\nExecStart={ path=/opt/keycloak/bin/kc.sh ; argv[]=kc.sh --password=top-secret ; }\nActiveState=running\n\nId=other.service\nExecStart=/bin/true\nActiveState=active"
    )
    assert result[0]["service"] == "custom-sso.service"
    assert "top-secret" not in json.dumps(result)


def test_native_database_companion_is_not_a_keycloak_server():
    result = common().unit_candidates(
        "Id=oneport-keycloak-postgres.service\nExecStart=/usr/lib/postgresql/18/bin/postgres -D /var/lib/oneport-keycloak-postgres\nActiveState=active"
    )
    assert result == []


def test_kubernetes_database_companion_is_not_a_keycloak_server():
    objects = resources()
    objects[-1]["metadata"]["name"] = "keycloak-postgresql-0"
    objects[-1]["spec"]["containers"][0]["image"] = "docker.io/bitnami/postgresql:18"
    report = discovery.cluster_report(
        [{"item": {"kind": "Pod"}, "resources": objects}], "cluster"
    )
    assert not report["candidates"]


def test_discovery_does_not_follow_metadata_supplied_jwks_url(monkeypatch):
    probe = common()
    calls = []

    def get(url, ca=None):
        calls.append(url)
        return {
            "issuer": "https://sso/realms/master",
            "jwks_uri": "https://outside/keys",
            "token_endpoint": "https://sso/realms/master/protocol/openid-connect/token",
        }

    monkeypatch.setattr(probe, "get_json", get)
    report = probe.endpoint_info("https://sso")
    assert report["checks"][0]["status"] == "fail"
    assert len(calls) == 1


def test_valid_metadata_does_not_claim_management_access(monkeypatch):
    probe = common()

    def get(url, ca=None):
        if url.endswith("certs"):
            return {"keys": [{"kty": "RSA", "kid": "key-1"}]}
        return {
            "issuer": "https://sso/realms/master",
            "jwks_uri": "https://sso/realms/master/protocol/openid-connect/certs",
            "token_endpoint": "https://sso/realms/master/protocol/openid-connect/token",
        }

    monkeypatch.setattr(probe, "get_json", get)
    report = probe.endpoint_info("https://sso")
    assert report["connection"]["admin_access"] == "not_checked"
    assert report["checks"][-1]["status"] == "unknown"


def test_controller_failure_prints_no_exception_payload(tmp_path, capsys, monkeypatch):
    file = tmp_path / "endpoint.yml"
    file.write_text(
        "schema_version: 1\nrequest_id: inspect-1\nendpoint:\n  base_url: https://sso.example.test\n"
    )
    monkeypatch.setattr(
        "migration.keycloak_infra.__main__.run",
        lambda *args: (_ for _ in ()).throw(OSError("sensitive-value")),
    )
    assert main(["inspect", "--config", str(file)]) == 1
    output = capsys.readouterr().out
    assert "sensitive-value" not in output
    assert json.loads(output)["reason_codes"] == ["CONTROLLER_EXECUTION_ERROR"]


def test_result_file_private_permissions(tmp_path):
    result = tmp_path / "result.json"
    runner.private_json(result, {"status": "complete"})
    assert result.stat().st_mode & 0o777 == 0o600


def test_missing_ansible_returns_structured_failure(raw, monkeypatch):
    c = config.validate_config(raw, "discover")
    monkeypatch.setattr(runner.shutil, "which", lambda _: None)
    result = runner.run("discover", c)
    assert result["status"] == "failed"
    assert "ANSIBLE_NOT_INSTALLED" in result["reason_codes"]


def test_fatal_installation_plays_stop_following_plays():
    import yaml

    for name in ("preflight", "install", "verify"):
        for play in yaml.safe_load(
            (config.ASSETS / "playbooks" / f"{name}.yml").read_text()
        ):
            if "hosts" in play:
                assert play["any_errors_fatal"] is True


def run_host_preflight(
    raw, monkeypatch, mutations=None, *, managed=False, occupied=False
):
    pytest.importorskip("ansible.module_utils.basic")
    import yaml

    probe = common()
    monkeypatch.setitem(sys.modules, "ansible.module_utils.keycloak_common", probe)
    spec = importlib.util.spec_from_file_location(
        "host_module_under_test", config.ASSETS / "library/keycloak_host_info.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    settings = config.validate_config(raw, "preflight")["install"]
    versions = yaml.safe_load((config.ASSETS / "versions.yml").read_text())[
        "kc_versions"
    ]
    node = {"uid": "uid-a", "machine_id": "machine-a"}
    manifest = {
        "install_id": settings["id"],
        "node_uid": "uid-a",
        "base_url": settings["base_url"],
        "https_port": settings["https_port"],
        "db_port": settings["db_port"],
        **versions,
    }
    facts = {
        "machine_id": "machine-a",
        "system_uuid": "",
        "os_id": "ubuntu",
        "os_version": "24.04",
        "systemd": True,
        "architecture": "x86_64",
        "memory_available_mb": 8192,
        "free_disk_mb": 20480,
        "managed_manifest": manifest if managed else None,
        "existing_accounts": [],
        "coverage": "complete",
        "existing_roots": [],
        "unit_conflict": False,
        "candidates": [],
        "managed_active": {
            "oneport-keycloak": managed,
            "oneport-keycloak-postgres": managed,
        },
    }
    facts.update(mutations or {})
    output = {}

    class FakeModule:
        def __init__(self):
            self.params = {"node": node, "install": settings, "versions": versions}

        def exit_json(self, **kwargs):
            output.update(kwargs)

    monkeypatch.setattr(module, "AnsibleModule", lambda **kwargs: FakeModule())
    monkeypatch.setattr(module, "host_info", lambda: facts)
    monkeypatch.setattr(module, "port_free", lambda port: not occupied)
    module.main()
    return output["report"]


@pytest.mark.parametrize(
    ("mutations", "failed_check"),
    [
        ({"machine_id": "different-host"}, "ssh_node_identity"),
        ({"os_id": "alpine"}, "supported_os"),
        ({"coverage": "unknown"}, "host_discovery"),
        ({"existing_roots": ["/opt/oneport-keycloak"]}, "managed_ownership"),
        (
            {"existing_accounts": [{"name": "oneport-keycloak", "uid": 1234}]},
            "service_account_ownership",
        ),
        ({"candidates": [{"service": "existing-sso"}]}, "existing_keycloak"),
        ({"free_disk_mb": 1}, "free_disk"),
        ({"memory_available_mb": 1}, "available_memory"),
    ],
)
def test_native_preflight_blocks_unsafe_host_conditions(
    raw, monkeypatch, mutations, failed_check
):
    result = run_host_preflight(raw, monkeypatch, mutations)
    assert any(
        c["name"] == failed_check and c["status"] == "fail" for c in result["checks"]
    )


def test_native_preflight_blocks_foreign_port_but_allows_owned_service(
    raw, monkeypatch
):
    result = run_host_preflight(raw, monkeypatch, occupied=True)
    assert any(
        c["name"] == "port_8443" and c["status"] == "fail" for c in result["checks"]
    )
    result = run_host_preflight(raw, monkeypatch, managed=True, occupied=True)
    assert all(c["status"] == "pass" for c in result["checks"])
