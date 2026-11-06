import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from migration.harbor_infra.ansible.module_utils.harbor_core import (
    Api,
    HarborError,
    check_oidc,
    configure,
    inspect,
)
from migration.harbor_infra.config import ASSETS, validate
from migration.harbor_infra.handoff import execute, state_directory
from migration.harbor_infra.runner import plan_digest, run
from migration.harbor_infra.verification import same_origin_upload, verify


def connected(raw, tls_server):
    server, certificate, fake = tls_server
    raw["endpoint"].update(url=server.url, ca_ref=str(certificate))
    raw["keycloak"].update(
        issuer_url=server.url + "/realms/demo", ca_ref=str(certificate)
    )
    return validate(raw, "preflight"), fake


def test_real_tls_and_certificate_verification(tls_server):
    server, certificate, _ = tls_server
    with pytest.raises(HarborError, match="TLS_OR_CONNECTION_FAILED"):
        inspect(Api(server.url))
    assert inspect(Api(server.url, certificate.read_text()))["product_confirmed"]
    with pytest.raises(HarborError, match="HTTP_REDIRECT_REJECTED"):
        Api(server.url, certificate.read_text()).call("GET", "/redirect")


def test_oidc_metadata_requires_matching_issuer(raw, tls_server):
    c, _ = connected(raw, tls_server)
    check_oidc(c["keycloak"], tls_server[1].read_text())
    c["keycloak"]["issuer_url"] += "/other"
    with pytest.raises(HarborError, match="OIDC_ISSUER_MISMATCH"):
        check_oidc(c["keycloak"], tls_server[1].read_text())


@pytest.mark.parametrize(
    "location",
    [
        "https://other.example/v2/upload",
        "//other.example/v2/upload",
        "http://registry.example.test/v2/upload",
        "/arbitrary/path",
        "/v2/../bad#fragment",
    ],
)
def test_upload_location_never_leaks_bearer_token(location):
    with pytest.raises(HarborError):
        same_origin_upload(Api("https://registry.example.test"), location)


def test_real_ansible_inspect_and_configure(raw, tls_server):
    c, fake = connected(raw, tls_server)
    result = run("inspect", c)
    assert result["status"] == "inspected", result
    assert result["reports"][0]["product_confirmed"] is True
    c["plan_digest"] = plan_digest(c)
    result = run("configure", c)
    assert result["status"] == "configured", result
    assert len(fake.robots) == 2
    output = json.dumps(result)
    for robot in fake.robots.values():
        assert robot["secret"] not in output
    assert "administrator-password-for-test-only" not in output
    assert "oidc-client-shared-secret" not in output
    assert "demo" in output
    result = run("configure", c)
    assert not result["changed"] and result["status"] == "configured", result


def test_real_tls_synthetic_push_pull_denial_and_cleanup(raw, tls_server):
    raw["projects"].append({"name": "isolated", "writer_days": 7, "reader_days": 30})
    c, fake = connected(raw, tls_server)
    api = Api(c["endpoint"]["url"], tls_server[1].read_text(), "admin", "password")
    configure(api, c, {"oidc_client_secret": "client-secret"}, state_directory(c))
    c["handoff"]["action"] = "prepare"
    result = execute(c)
    c["handoff"].update(action="ack", receipt_digest=result["sha256"])
    execute(c)
    c["verification"]["image_tests"] = True
    result = verify(
        c, {"endpoint_ca": tls_server[1].read_text(), "admin_password": "password"}
    )
    assert result["status"] == "ready", result
    assert (
        len(
            [
                v
                for v in result["checks"]
                if v["name"] == "cross_project_pull_denied" and v["status"] == "passed"
            ]
        )
        == 2
    )
    assert not fake.manifests
    assert (
        next(v for v in result["checks"] if v["name"] == "oidc_browser_login")["status"]
        == "not_run"
    )


def test_unperformed_verification_never_claims_ready(raw, tls_server):
    c, _ = connected(raw, tls_server)
    api = Api(c["endpoint"]["url"], tls_server[1].read_text())
    configure(api, c, {"oidc_client_secret": "secret-client"}, state_directory(c))
    result = verify(c, {"endpoint_ca": tls_server[1].read_text()})
    assert result["status"] == "partially_ready"
    assert any(
        x["name"] == "image_permission_tests" and x["status"] == "not_run"
        for x in result["checks"]
    )


def test_all_ansible_playbooks_have_valid_syntax(tmp_path):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "all": {
                    "children": {
                        "harbor_nodes": {"hosts": {}},
                        "controllers": {
                            "hosts": {"controller": {"ansible_connection": "local"}}
                        },
                    }
                }
            }
        )
    )
    env = {
        **os.environ,
        "ANSIBLE_CONFIG": str(ASSETS / "ansible.cfg"),
        "ANSIBLE_LIBRARY": str(ASSETS / "library"),
        "ANSIBLE_HOME": str(tmp_path / "ansible-home"),
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-temp"),
    }
    command = str(Path(sys.executable).parent / "ansible-playbook")
    for book in (ASSETS / "playbooks").glob("*.yml"):
        p = subprocess.run(
            [command, "-i", str(inventory), str(book), "--syntax-check"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert p.returncode == 0, p.stdout + p.stderr


def test_runtime_probe_uses_temporary_objects_and_uid_cleanup(raw, monkeypatch):
    from types import SimpleNamespace as N

    from kubernetes import client, config
    from migration.harbor_infra.verification import runtime_probe

    c = validate(raw, "preflight")
    c.update(
        cluster={"kubeconfig_ref": "unused", "context": "selected"},
        trust={"targets": ["worker"], "restart_approved": []},
    )
    c["verification"]["test_namespace"] = "verification"
    calls = []

    class Core:
        def create_namespaced_secret(self, namespace, body, **kwargs):
            calls.append(("secret", namespace, body))
            return N(metadata=N(uid="secret-uid"))

        def create_namespaced_pod(self, namespace, body, **kwargs):
            calls.append(("pod", namespace, body))
            return N(metadata=N(uid="pod-uid"))

        def read_namespaced_pod(self, *args, **kwargs):
            return N(
                status=N(
                    container_statuses=[N(image_id="containerd://sha256:manifest")]
                )
            )

        def delete_namespaced_pod(self, *args, **kwargs):
            calls.append(("delete_pod", kwargs["body"]))

        def delete_namespaced_secret(self, *args, **kwargs):
            calls.append(("delete_secret", kwargs["body"]))

    monkeypatch.setattr(config, "load_kube_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(client, "CoreV1Api", lambda *args: Core())
    result = runtime_probe(
        c,
        "demo",
        "trial",
        "sha256:manifest",
        "sha256:config",
        {"username": "robot", "secret": "PRIVATE"},
    )
    assert result[0]["status"] == "passed"
    pod = next(v[2] for v in calls if v[0] == "pod")
    assert pod["spec"]["nodeName"] == "worker"
    assert pod["spec"]["automountServiceAccountToken"] is False
    assert (
        "delete_pod",
        {"gracePeriodSeconds": 0, "preconditions": {"uid": "pod-uid"}},
    ) in calls
    assert ("delete_secret", {"preconditions": {"uid": "secret-uid"}}) in calls


def test_actual_ansible_result_schema(raw, tls_server):
    import jsonschema

    c, _ = connected(raw, tls_server)
    schema = json.loads(
        (Path(__file__).parents[2] / "docs/harbor-infra/result.schema.json").read_text()
    )
    jsonschema.validate(run("inspect", c), schema)


@pytest.mark.parametrize("drift", ["public", "proxy", "oidc"])
def test_verification_detects_destination_or_provider_drift(raw, tls_server, drift):
    c, fake = connected(raw, tls_server)
    api = Api(c["endpoint"]["url"], tls_server[1].read_text())
    configure(api, c, {"oidc_client_secret": "secret-client"}, state_directory(c))
    if drift == "public":
        fake.projects["demo"]["metadata"]["public"] = "true"
    if drift == "proxy":
        fake.projects["demo"]["registry_id"] = 123
    if drift == "oidc":
        fake.configuration["oidc_endpoint"] = "https://other.example/realms/evil"
    result = verify(c, {"endpoint_ca": tls_server[1].read_text()})
    assert result["status"] == "partially_ready"
    assert any(v["status"] == "failed" for v in result["checks"])


def test_real_ansible_host_module_blocks_wrong_identity_before_changes(tmp_path):
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "all": {
                    "hosts": {
                        "local": {
                            "ansible_connection": "local",
                            "ansible_python_interpreter": sys.executable,
                        }
                    }
                }
            }
        )
    )
    playbook = tmp_path / "host-check.yml"
    playbook.write_text("""- hosts: all
  gather_facts: false
  tasks:
    - harbor_host:
        operation: discover
        config:
          nodes: [{name: probe, uid: expected-uid}]
        node: {name: probe, uid: expected-uid, machine_id: deliberately-wrong-machine}
""")
    env = {
        **os.environ,
        "ANSIBLE_CONFIG": str(ASSETS / "ansible.cfg"),
        "ANSIBLE_LIBRARY": str(ASSETS / "library"),
        "ANSIBLE_MODULE_UTILS": str(ASSETS / "module_utils"),
        "ANSIBLE_HOME": str(tmp_path / "home"),
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / "local"),
    }
    command = str(Path(sys.executable).parent / "ansible-playbook")
    p = subprocess.run(
        [command, "-i", str(inventory), str(playbook)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert p.returncode != 0
    assert "LINUX_REQUIRED" in p.stdout or "HOST_IDENTITY_MISMATCH" in p.stdout
    assert "MODULE FAILURE" not in p.stdout
