import base64
import copy
import datetime
import json
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@pytest.fixture
def raw(tmp_path):
    def protected(name, value):
        p = tmp_path / name
        p.write_text(value)
        p.chmod(0o600)
        return str(p)

    admin = protected("admin", "administrator-password-for-test-only-12345")
    return {
        "schema_version": 1,
        "request_id": "request-1",
        "organization_id": "org-1",
        "target": {"mode": "reuse", "installation_id": "registry-1"},
        "endpoint": {
            "url": "https://registry.example.test:8444",
            "admin_password_ref": admin,
        },
        "keycloak": {
            "issuer_url": "https://identity.example.test/realms/demo",
            "client_id": "harbor",
            "client_secret_ref": protected(
                "oidc", "oidc-client-shared-secret-test-only-12345"
            ),
        },
        "projects": [{"name": "demo", "writer_days": 7, "reader_days": 30}],
        "handoff": {"directory": str(tmp_path / "handoff")},
    }


class FakeApi:
    def __init__(self):
        self.configuration = {"auth_mode": "db_auth"}
        self.projects = {}
        self.robots = {}
        self.users = [{"username": "admin"}]
        self.groups = []
        self.members = {}
        self.calls = []
        self.lose_robot_response = False
        self.manifests = {}
        self.blobs = {}
        self.sequence = 0

    def call(self, method, path, body=None, expected=(200,), authenticated=True):
        self.calls.append((method, path, copy.deepcopy(body)))
        path = urlsplit(path).path
        if path == "/api/v2.0/configurations":
            if method == "PUT":
                self.configuration.update(body)
                return None
            return {k: {"value": v} for k, v in self.configuration.items()}
        if path == "/api/v2.0/users":
            return copy.deepcopy(self.users)
        if path == "/api/v2.0/health":
            return {
                "status": "healthy",
                "components": [{"name": "core", "status": "healthy"}],
            }
        if path == "/api/v2.0/systeminfo":
            return {"harbor_version": "v2.14.6"}
        if path == "/api/v2.0/projects" and method == "POST":
            self.projects[body["project_name"]] = {
                "project_id": len(self.projects) + 1,
                "name": body["project_name"],
                "metadata": body["metadata"],
            }
            return None
        if path.startswith("/api/v2.0/projects/"):
            bits = path.split("/")
            name = bits[4]
            if len(bits) == 5:
                return copy.deepcopy(self.projects.get(name))
            if bits[5] == "members":
                if method == "GET":
                    return copy.deepcopy(self.members.get(name, []))
                self.members.setdefault(name, []).append(
                    {
                        "entity_type": "g",
                        "entity_id": body["member_group"]["id"],
                        "role_id": body["role_id"],
                    }
                )
                return None
            if "artifacts" in path and method == "DELETE":
                self.manifests.pop(name + "/" + bits[6], None)
                return None
        if path == "/api/v2.0/usergroups":
            if method == "GET":
                return copy.deepcopy(self.groups)
            self.groups.append({**body, "id": len(self.groups) + 1})
            return None
        if path == "/api/v2.0/robots":
            if method == "GET":
                return [copy.deepcopy(v) for v in self.robots.values()]
            rid = len(self.robots) + 1
            robot = {
                **body,
                "id": rid,
                "name": "robot$"
                + body["permissions"][0]["namespace"]
                + "+"
                + body["name"],
                "expires_at": int(time.time()) + body["duration"] * 86400,
            }
            self.robots[rid] = robot
            if self.lose_robot_response:
                self.lose_robot_response = False
                from migration.harbor_infra.ansible.module_utils.harbor_core import (
                    HarborError,
                )

                raise HarborError("TLS_OR_CONNECTION_FAILED")
            return copy.deepcopy(robot)
        if path.startswith("/api/v2.0/robots/"):
            return copy.deepcopy(self.robots[int(path.rsplit("/", 1)[1])])
        raise AssertionError((method, path))

    def pages(self, path):
        return self.call("GET", path)


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def tls_server(tmp_path, api):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    import ipaddress

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
        )
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certpath = tmp_path / "server.crt"
    keypath = tmp_path / "server.key"
    certpath.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keypath.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    keypath.chmod(0o600)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code, data=None, headers=None, raw=False):
            payload = (
                data if raw else json.dumps(data).encode() if data is not None else b""
            )
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def handle_request(self):
            path = urlsplit(self.path).path
            if path == "/redirect":
                return self.respond(
                    302, headers={"Location": "https://example.invalid/secret"}
                )
            if path.endswith("/.well-known/openid-configuration"):
                return self.respond(
                    200,
                    {
                        "issuer": server.url + "/realms/demo",
                        "authorization_endpoint": server.url + "/authorize",
                        "token_endpoint": server.url + "/token",
                        "jwks_uri": server.url + "/keys",
                    },
                )
            if path == "/service/token":
                authorization = self.headers.get("Authorization", "")
                username, password = (
                    base64.b64decode(authorization.split()[1]).decode().split(":", 1)
                )
                matched = next(
                    (
                        r
                        for r in api.robots.values()
                        if r["name"] == username and r["secret"] == password
                    ),
                    None,
                )
                return (
                    self.respond(200, {"token": username})
                    if matched
                    else self.respond(401)
                )
            size = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(size) if size else b""
            if path.startswith("/v2/"):
                username = self.headers.get("Authorization", "").removeprefix("Bearer ")
                robot = next(
                    (r for r in api.robots.values() if r["name"] == username), None
                )
                bits = path.split("/")
                project = bits[2]
                repo = project + "/" + bits[3]
                if not robot or robot["permissions"][0]["namespace"] != project:
                    return self.respond(401)
                actions = {a["action"] for a in robot["permissions"][0]["access"]}
                if self.command in ("POST", "PUT") and "push" not in actions:
                    return self.respond(401)
                if "/blobs/uploads/" in path:
                    if self.command == "POST":
                        return self.respond(
                            202,
                            headers={
                                "Location": server.url
                                + "/v2/"
                                + repo
                                + "/blobs/uploads/test?state=1"
                            },
                        )
                    api.blobs[parse_qs(urlsplit(self.path).query)["digest"][0]] = (
                        payload
                    )
                    return self.respond(201)
                if self.command == "PUT":
                    api.manifests[repo] = payload
                    return self.respond(201)
                if repo not in api.manifests:
                    return self.respond(404)
                return self.respond(200, api.manifests[repo], raw=True)
            body = json.loads(payload) if payload else None
            reply = api.call(self.command, self.path, body)
            if reply is None and self.command == "GET":
                return self.respond(404)
            return self.respond(201 if self.command == "POST" else 200, reply)

        do_GET = do_POST = do_PUT = do_HEAD = do_DELETE = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certpath, keypath)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.url = "https://127.0.0.1:" + str(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, certpath, api
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()
