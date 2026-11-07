from __future__ import annotations

import json
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from migration.identity.adapters import Http, KubernetesHttp, Targets
from migration.identity.common import IdentityError, private_write
from migration.identity.verify import kubernetes_probe, openbao_probe


@pytest.fixture
def https_api(tmp_path):
    cert, key, settings = (
        tmp_path / name for name in ("server.crt", "server.key", "openssl.cnf")
    )
    settings.write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:TRUE\nkeyUsage=digitalSignature,keyEncipherment,keyCertSign\n"
    )
    subprocess.run(
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
            "-config",
            str(settings),
        ],
        check=True,
        capture_output=True,
    )
    calls, resources = [], {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            path = urlsplit(self.path).path
            payload = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"null"
            )
            calls.append((self.command, path, payload, dict(self.headers)))
            status, result = 200, {}
            if path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/credential-leak")
                self.end_headers()
                return
            if path == "/api/v1/namespaces/orders-dev":
                result = {"metadata": {"name": "orders-dev"}}
            elif path == "/apis/authentication.k8s.io/v1/selfsubjectreviews":
                assert self.headers["Authorization"] == "Bearer REAL-USER-TOKEN"
                result = {
                    "status": {
                        "userInfo": {
                            "username": "keycloak:subject-1",
                            "groups": ["kc:grant-1"],
                        }
                    }
                }
            elif path == "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews":
                result = {
                    "status": {
                        "allowed": payload["spec"]["resourceAttributes"]["verb"]
                        == "get"
                    }
                }
            elif path.startswith(
                "/apis/rbac.authorization.k8s.io/v1/namespaces/orders-dev/"
            ):
                if self.command == "POST":
                    resources[path + "/" + payload["metadata"]["name"]] = payload
                    status, result = 201, payload
                elif self.command == "DELETE":
                    resources.pop(path, None)
                    result = {}
                elif path in resources:
                    result = resources[path]
                else:
                    status = 404
            elif path == "/v1/sys/auth":
                result = {
                    "data": {
                        "identity-test/": {
                            "type": "jwt",
                            "description": "identity:test-orders",
                        }
                    }
                }
            elif path == "/v1/sys/mounts":
                result = {"data": {"kv/": {"type": "kv", "options": {"version": "2"}}}}
            elif path == "/v1/auth/identity-test/login":
                assert payload["jwt"] == "SIGNED-USER-TOKEN"
                result = {
                    "auth": {
                        "client_token": "TEMP-BAO-TOKEN",
                        "metadata": {"source_subject": "subject-1"},
                    }
                }
            elif path == "/v1/auth/token/revoke-self":
                assert self.headers["X-Vault-Token"] == "TEMP-BAO-TOKEN"
            elif path.startswith("/v1/kv/data/"):
                assert self.headers["X-Vault-Token"] == "TEMP-BAO-TOKEN"
                if path.endswith("forbidden"):
                    status = 403
                else:
                    result = {
                        "data": {"data": {"password": "NEVER-REPORT-THIS-SECRET"}}
                    }
            elif path.startswith("/v1/sys/policies/acl/"):
                if self.command == "PUT":
                    resources[path] = payload
                elif self.command == "DELETE":
                    resources.pop(path, None)
                elif path in resources:
                    result = {"data": resources[path]}
                else:
                    status = 404
            elif path == "/admin/realms" and self.command == "POST":
                status = 201
            elif path.endswith("/users/profile"):
                if self.command == "PUT":
                    resources["profile"] = payload
                else:
                    result = resources.get(
                        "profile", {"attributes": [{"name": "username"}]}
                    )
            elif path.startswith("/admin/realms/"):
                if "/users/" in path and path.endswith("/groups"):
                    result = []
                elif self.command == "GET" and path.endswith("/users"):
                    result = resources.get("kc_users", [])
                elif self.command == "POST" and path.endswith("/users"):
                    resources.setdefault("kc_users", []).append(
                        {"id": "user-uuid", **payload}
                    )
                    status = 201
                elif self.command == "GET" and "/users/" in path:
                    result = resources["kc_users"][0]
                else:
                    result = []
            else:
                status = 404
            body = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_PUT = do_DELETE = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{server.server_port}", str(cert), calls, resources
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def configure_local(identity_config, https_api, tmp_path, monkeypatch):
    base, ca, _, _ = https_api
    for kind, env in (("keycloak", "KC_TOKEN"), ("openbao", "BAO_TOKEN")):
        identity_config["targets"][kind].update(base_url=base, ca_file=ca)
        monkeypatch.setenv(env, "ADMIN-" + kind)
    kubeconfig = tmp_path / "kubeconfig.json"
    private_write(
        kubeconfig,
        {
            "contexts": [
                {"name": "onprem", "context": {"cluster": "cluster", "user": "admin"}}
            ],
            "clusters": [
                {
                    "name": "cluster",
                    "cluster": {"server": base, "certificate-authority": ca},
                }
            ],
            "users": [{"name": "admin", "user": {"token": "ADMIN-KUBE-TOKEN"}}],
        },
    )
    identity_config["targets"]["kubernetes"]["kubeconfig"] = str(kubeconfig)


def test_actual_https_transport_validates_trust_and_refuses_redirects(
    https_api, monkeypatch
):
    base, ca, calls, _ = https_api
    monkeypatch.setenv("TOKEN", "DO-NOT-LEAK")
    api = Http(base, "TOKEN", ca)
    with pytest.raises(IdentityError, match="TARGET_HTTP_302"):
        api.request("GET", "/redirect")
    assert not any(c[1] == "/credential-leak" for c in calls)
    with pytest.raises(IdentityError, match="TARGET_TRANSPORT_ERROR"):
        Http(base, "TOKEN").request("GET", "/anything")
    assert api.session.trust_env is False


def test_native_kube_and_bao_resource_roundtrip(
    identity_config, identity_plan, https_api, tmp_path, monkeypatch
):
    configure_local(identity_config, https_api, tmp_path, monkeypatch)
    targets = Targets(identity_config, identity_plan)
    for kind in ("k8s_role", "k8s_binding", "bao_policy"):
        resource = next(r for r in identity_plan["resources"] if r["kind"] == kind)
        assert targets.read(resource) is None
        targets.write(resource)
        assert targets.read(resource) == resource["payload"]
        if kind != "k8s_role":
            targets.delete(resource)
            assert targets.read(resource) is None


def test_keycloak_users_created_disabled_without_credentials(
    identity_config, identity_plan, https_api, tmp_path, monkeypatch
):
    configure_local(identity_config, https_api, tmp_path, monkeypatch)
    targets = Targets(identity_config, identity_plan)
    resource = next(r for r in identity_plan["resources"] if r["kind"] == "kc_user")
    targets.write(resource)
    assert targets.read(resource) == resource["payload"]
    payload = next(
        c[2] for c in https_api[2] if c[0] == "POST" and c[1].endswith("/users")
    )
    assert payload["enabled"] is False and "credentials" not in payload
    assert "CONFIGURE_TOTP" in payload["requiredActions"]


def test_new_realm_keeps_unmanaged_identity_attributes(
    identity_config, identity_plan, https_api, tmp_path, monkeypatch
):
    configure_local(identity_config, https_api, tmp_path, monkeypatch)
    targets = Targets(identity_config, identity_plan)
    realm = next(r for r in identity_plan["resources"] if r["kind"] == "kc_realm")
    targets.write(realm)
    calls = [(c[0], c[1]) for c in https_api[2]]
    profile_path = (
        "/admin/realms/"
        + identity_config["targets"]["keycloak"]["realm"]
        + "/users/profile"
    )
    assert calls[-3:] == [
        ("POST", "/admin/realms"),
        ("GET", profile_path),
        ("PUT", profile_path),
    ]
    assert https_api[3]["profile"] == {
        "attributes": [{"name": "username"}],
        "unmanagedAttributePolicy": "ADMIN_EDIT",
    }


def test_real_token_probe_never_uses_admin_kube_identity(
    identity_config, https_api, tmp_path, monkeypatch
):
    configure_local(identity_config, https_api, tmp_path, monkeypatch)
    monkeypatch.setenv("USER_TOKEN", "REAL-USER-TOKEN")
    c = {
        "token_env": "USER_TOKEN",
        "expected_subject": "subject-1",
        "group": "grant-1",
        "namespace": "orders-dev",
        "verb": "get",
        "resource": "pods",
        "api_group": "",
        "allowed": True,
    }
    assert (
        kubernetes_probe(c, identity_config)
        == "authenticated_self_subject_access_review"
    )
    c.update(verb="delete", allowed=False)
    kubernetes_probe(c, identity_config)
    assert all(
        call[3]["Authorization"] == "Bearer REAL-USER-TOKEN" for call in https_api[2]
    )


@pytest.mark.parametrize(
    "allowed,path", [(True, "kv/data/orders"), (False, "kv/data/forbidden")]
)
def test_bao_probe_reads_and_revokes_ephemeral_token(
    identity_config, https_api, tmp_path, monkeypatch, allowed, path
):
    configure_local(identity_config, https_api, tmp_path, monkeypatch)
    monkeypatch.setenv("USER_TOKEN", "SIGNED-USER-TOKEN")
    result = openbao_probe(
        {
            "token_env": "USER_TOKEN",
            "expected_subject": "subject-1",
            "role": "reader",
            "path": path,
            "allowed": allowed,
        },
        identity_config,
    )
    assert result == "authenticated_secret_read"
    assert https_api[2][-1][1] == "/v1/auth/token/revoke-self"
    assert "NEVER-REPORT-THIS-SECRET" not in result


def test_exec_kubeconfigs_rejected_but_user_probe_has_no_admin_fallback(
    identity_config, https_api, tmp_path, monkeypatch
):
    configure_local(identity_config, https_api, tmp_path, monkeypatch)
    path = identity_config["targets"]["kubernetes"]["kubeconfig"]
    from migration.identity.common import load

    raw = load(path)
    raw["users"][0]["user"] = {
        "exec": {"command": "do-not-execute"},
        "client-certificate": "/not/read",
        "client-key": "/not/read",
    }
    private_write(path, raw)
    with pytest.raises(IdentityError, match="STATIC_KUBECONFIG_REQUIRED"):
        KubernetesHttp(identity_config["targets"]["kubernetes"])
    probe = KubernetesHttp(
        identity_config["targets"]["kubernetes"], user_token="real-token"
    )
    assert probe.session.cert is None and probe.headers == {
        "Authorization": "Bearer real-token"
    }


def test_new_bao_auth_mount_default_configuration_is_not_drift(identity_plan):
    from types import SimpleNamespace

    resource = next(r for r in identity_plan["resources"] if r["kind"] == "bao_config")
    target = Targets.__new__(Targets)
    target.bao = SimpleNamespace(
        request=lambda *args, **kwargs: {
            "data": {
                "oidc_discovery_url": "",
                "bound_issuer": "",
                "jwt_supported_algs": [],
            }
        }
    )
    assert target.read(resource) is None


def test_bao_existing_other_trust_configuration_is_a_conflict(identity_plan):
    from types import SimpleNamespace

    resource = next(r for r in identity_plan["resources"] if r["kind"] == "bao_config")
    target = Targets.__new__(Targets)
    target.bao = SimpleNamespace(
        request=lambda *args, **kwargs: {
            "data": {
                "oidc_discovery_url": "https://other-issuer.test",
                "bound_issuer": "",
                "jwt_supported_algs": [],
            }
        }
    )
    assert target.read(resource) != resource["payload"]
