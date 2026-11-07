"""Execute real packaged Ansible against an isolated HTTPS identity fixture."""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml
from migration.keycloak_infra.config import ASSETS, load_config
from migration.keycloak_infra.runner import run


@pytest.fixture
def tls_endpoint(tmp_path):
    if not shutil.which("ansible-playbook") or not shutil.which("openssl"):
        pytest.skip("Ansible/OpenSSL required for execution test")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    proc = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            issuer = self.server.base_url + "/realms/master"
            api_lists = {
                "/api/v1": [
                    ("nodes", "Node", False),
                    ("pods", "Pod", True),
                    ("services", "Service", True),
                ],
                "/apis/apps/v1": [
                    ("deployments", "Deployment", True),
                    ("replicasets", "ReplicaSet", True),
                    ("statefulsets", "StatefulSet", True),
                ],
                "/apis/networking.k8s.io/v1": [("ingresses", "Ingress", True)],
            }
            if self.path == "/version":
                data = {"major": "1", "minor": "37", "gitVersion": "v1.37.2"}
            elif self.path == "/api":
                data = {"kind": "APIVersions", "apiVersion": "v1", "versions": ["v1"]}
            elif self.path == "/apis":
                data = {
                    "kind": "APIGroupList",
                    "apiVersion": "v1",
                    "groups": [
                        {
                            "name": group,
                            "versions": [
                                {"groupVersion": group + "/v1", "version": "v1"}
                            ],
                            "preferredVersion": {
                                "groupVersion": group + "/v1",
                                "version": "v1",
                            },
                        }
                        for group in ("apps", "networking.k8s.io")
                    ],
                }
            elif self.path in api_lists:
                version = self.path.removeprefix("/apis/").removeprefix("/api/")
                data = {
                    "kind": "APIResourceList",
                    "apiVersion": "v1",
                    "groupVersion": version,
                    "resources": [
                        {
                            "name": name,
                            "singularName": "",
                            "kind": kind,
                            "namespaced": ns,
                            "verbs": ["get", "list"],
                        }
                        for name, kind, ns in api_lists[self.path]
                    ],
                }
            elif self.path == "/api/v1/nodes":
                data = {
                    "apiVersion": "v1",
                    "kind": "NodeList",
                    "metadata": {},
                    "items": [
                        {
                            "apiVersion": "v1",
                            "kind": "Node",
                            "metadata": {"name": "fixture-node", "uid": "fixture-uid"},
                            "status": {"nodeInfo": {"machineID": "fixture-machine"}},
                        }
                    ],
                }
            elif self.path == "/api/v1/pods":
                data = {
                    "apiVersion": "v1",
                    "kind": "PodList",
                    "metadata": {},
                    "items": [
                        {
                            "apiVersion": "v1",
                            "kind": "Pod",
                            "metadata": {
                                "name": "fixture-sso",
                                "namespace": "identity",
                                "uid": "fixture-pod",
                            },
                            "spec": {
                                "nodeName": "fixture-node",
                                "containers": [
                                    {
                                        "name": "sso",
                                        "image": "quay.io/keycloak/keycloak:26.8.2",
                                    }
                                ],
                            },
                        }
                    ],
                }
            elif self.path.startswith(("/api/", "/apis/")):
                data = {"apiVersion": "v1", "kind": "List", "metadata": {}, "items": []}
            elif self.path.endswith("openid-configuration"):
                data = {
                    "issuer": issuer,
                    "jwks_uri": issuer + "/protocol/openid-connect/certs",
                    "token_endpoint": issuer + "/protocol/openid-connect/token",
                }
            elif self.path.endswith("/certs"):
                data = {"keys": [{"kty": "RSA", "kid": "fixture-1"}]}
            elif self.path == "/health/ready":
                data = {"status": "UP"}
            else:
                self.send_error(404)
                return
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.base_url = f"https://localhost:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "base_url": server.base_url,
            "certificate_file": str(cert),
            "private_key_file": str(key),
            "ca_file": str(cert),
        }
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_real_ansible_inspection_returns_structured_readiness(tls_endpoint, tmp_path):
    path = tmp_path / "inspect.yml"
    endpoint = {k: tls_endpoint[k] for k in ("base_url", "ca_file")}
    endpoint["management_url"] = endpoint["base_url"]
    path.write_text(
        yaml.safe_dump(
            {"schema_version": 1, "request_id": "inspect-tls", "endpoint": endpoint}
        )
    )
    result = run("inspect", load_config(path, "inspect"))
    assert result["status"] == "complete", result
    assert result["connection"]["admin_access"] == "not_checked"
    assert {x["name"] for x in result["checks"] if x["status"] == "pass"} == {
        "oidc_discovery",
        "signing_keys",
        "readiness",
    }
    assert not result["changes"]


def test_real_ansible_rejects_untrusted_certificate(tls_endpoint, tmp_path):
    path = tmp_path / "inspect.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "request_id": "untrusted-tls",
                "endpoint": {"base_url": tls_endpoint["base_url"]},
            }
        )
    )
    result = run("inspect", load_config(path, "inspect"))
    assert result["status"] == "failed", result
    assert "CHECK_FAILED" in result["reason_codes"]


def test_real_ansible_kubernetes_discovery_preserves_missing_ssh_coverage(
    tls_endpoint, tmp_path
):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": "fixture",
                "clusters": [
                    {
                        "name": "fixture",
                        "cluster": {
                            "server": tls_endpoint["base_url"],
                            "certificate-authority": tls_endpoint["ca_file"],
                        },
                    }
                ],
                "users": [{"name": "fixture", "user": {"token": "fixture-token"}}],
                "contexts": [
                    {
                        "name": "fixture",
                        "context": {"cluster": "fixture", "user": "fixture"},
                    }
                ],
            }
        )
    )
    path = tmp_path / "discover.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "request_id": "discover-fixture",
                "cluster": {
                    "id": "fixture",
                    "kubeconfig": str(kubeconfig),
                    "context": "fixture",
                },
            }
        )
    )
    result = run("discover", load_config(path, "discover"))
    assert result["status"] == "partial", result
    assert len(result["candidates"]) == 1, result
    assert result["candidates"][0]["nodes"] == ["fixture-node"]
    assert not result["events"], result
    assert "NODE_SSH_INFORMATION_MISSING_OR_STALE" in result["reason_codes"]


def test_certificate_module_checks_supplied_key_and_name(tls_endpoint, tmp_path):
    # Exercise the same module used before all installation changes.
    playbook = tmp_path / "certificate.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "tasks": [
                        {
                            "name": "Check fixture certificate",
                            "keycloak_certificate_info": tls_endpoint,
                            "register": "certificate",
                        },
                        {
                            "name": "Require all fixture checks",
                            "ansible.builtin.assert": {
                                "that": "certificate.report.checks | rejectattr('status', 'equalto', 'pass') | list | length == 0"
                            },
                        },
                    ],
                }
            ]
        )
    )
    env = {
        **os.environ,
        "ANSIBLE_LIBRARY": str(ASSETS / "library"),
        "ANSIBLE_MODULE_UTILS": str(ASSETS / "module_utils"),
    }
    proc = subprocess.run(
        [
            "ansible-playbook",
            "-i",
            "localhost,",
            "-c",
            "local",
            str(playbook),
            "-e",
            "ansible_python_interpreter=" + shutil.which("python"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
