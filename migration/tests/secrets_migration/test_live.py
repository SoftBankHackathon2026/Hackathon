"""Real TLS OpenBao using the installer policy verbatim; no Docker or remote account."""

import os

import pytest

pytest.importorskip("ansible")
pytest.importorskip("cryptography")

from migration.openbao_infra.runner import aggregate
from migration.secrets.common import SecretsError, digest, save, seal_artifact
from migration.secrets.engine import run
from migration.secrets.plan import build_plan, preflight
from migration.secrets.state import State
from migration.secrets.target import OpenBao
from openbao_infra.conftest import manage
from openbao_infra.test_live import server  # noqa: F401 -- shared real-server fixture

from .conftest import CANARY, PATH


@pytest.fixture
def live(scenario, server, monkeypatch):  # noqa: F811 -- shared pytest fixture
    s = scenario
    path, api, proc, start = server
    c = {
        "target": {"installation_id": "test", "mode": "reuse"},
        "endpoint": {"url": api.endpoint, "product_confirmed": True},
        "remote_ca": str(path / "server.crt"),
        "access": {
            "mount": "kv",
            "prefix": "migration",
            "token_ttl_seconds": 300,
            "secret_id_ttl_seconds": 60,
        },
        "applications": [],
        "credentials": {},
    }
    manager = manage.Manager(c, path / "state", api)
    manager.bootstrap()
    manager.authenticate()
    configured, changed = manager.configure()
    manager.verify_seed()
    verified, _ = manager.verify()
    save(
        s.config["preparation"]["configure_result"],
        aggregate(
            "configure",
            {"request_id": "test"},
            [{"host": "local", "report": configured, "changed": changed}],
            0,
        ),
    )
    save(
        s.config["preparation"]["verify_result"],
        aggregate(
            "verify", {"request_id": "test"}, [{"host": "local", "report": verified}], 0
        ),
    )
    health = api.health()
    s.config["target"].update(
        endpoint=api.endpoint,
        ca_file=str(path / "server.crt"),
        cluster_id=health["cluster_id"],
        expected_version=health["version"],
    )
    s.identity_config["targets"]["openbao"]["base_url"] = api.endpoint
    s.identity_plan["config_hash"] = digest(s.identity_config)
    seal_artifact(s.identity_plan, "plan_hash")
    save(s.config["identity"]["config"], s.identity_config)
    save(s.config["identity"]["plan"], s.identity_plan)
    token_client = manager.migration_token()
    monkeypatch.setenv("TEST_BAO_TOKEN", token_client.token)
    s.plan = build_plan(s.config, s.inventory, s.mapping)
    s.target = OpenBao(s.config["target"])
    s.prepared = preflight(s.config, s.plan, s.source, s.target)
    s.manager, s.api = manager, api
    return s


@pytest.mark.secrets_live
def test_actual_installer_token_copies_without_sys_mounts_or_metadata_writes(live):
    s = live
    with State(s.state) as state:
        assert (
            run(s.config, s.plan, s.prepared, s.source, s.target, state)["copy_status"]
            == "verified"
        )
        assert (
            run(s.config, s.plan, s.prepared, s.source, s.target, state)["copy_status"]
            == "verified"
        )
    assert s.target.read_value(PATH).data == CANARY
    assert s.target.read_value(PATH, "previous").data == b"previous"
    for method, path in (
        ("GET", "sys/mounts"),
        ("POST", "kv/metadata/" + PATH),
        ("DELETE", "kv/data/" + PATH),
        ("GET", "kv/data/outside/probe"),
    ):
        with pytest.raises(SecretsError, match="ACCESS_DENIED"):
            s.target.http.request(method, path, {} if method == "POST" else None)
    assert CANARY not in s.state.read_bytes()


@pytest.mark.secrets_live
def test_actual_root_token_and_changed_credential_are_rejected(live, monkeypatch):
    s = live
    monkeypatch.setenv("TEST_BAO_TOKEN", s.api.token)
    with pytest.raises(SecretsError, match="CREDENTIAL_CHANGED"):
        s.target.inspect([PATH])
    root = OpenBao(s.config["target"])
    with pytest.raises(SecretsError, match="NONROOT"):
        root.inspect([PATH])


@pytest.mark.secrets_live
def test_actual_read_only_token_cannot_copy(live, monkeypatch):
    s = live
    for capabilities in ('["read"]', '["create", "read", "update", "delete"]'):
        s.api.call(
            "PUT",
            "sys/policies/acl/test-reader",
            {
                "policy": 'path "kv/data/migration/*" { capabilities = '
                + capabilities
                + " }"
            },
        )
        token = s.api.call(
            "POST", "auth/token/create", {"policies": ["test-reader"], "ttl": "5m"}
        )["auth"]["client_token"]
        monkeypatch.setenv("TEST_BAO_TOKEN", token)
        readonly = OpenBao(s.config["target"])
        with pytest.raises(SecretsError, match="PERMISSION_MISSING"):
            readonly.inspect([PATH])


@pytest.mark.secrets_live
def test_actual_untrusted_ca_rejected(live):
    import ssl

    s = live
    # Real certificate validation fails if the test CA is not trusted.
    from urllib.request import HTTPSHandler, Request, build_opener

    from migration.secrets.target import NoRedirect

    opener = build_opener(
        NoRedirect(), HTTPSHandler(context=ssl.create_default_context())
    )
    with pytest.raises(OSError):
        opener.open(
            Request(s.config["target"]["endpoint"] + "/v1/sys/health"), timeout=5
        )


@pytest.mark.secrets_live
def test_real_tls_redirect_does_not_forward_token(live):
    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    from migration.secrets.target import Http

    s = live
    received = []
    servers = []
    threads = []

    class Sink(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get("X-Vault-Token"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    class Redirect(Sink):
        def do_GET(self):
            self.send_response(307)
            self.send_header(
                "Location", "https://127.0.0.1:" + str(servers[0].server_port) + "/sink"
            )
            self.end_headers()

    try:
        for handler in (Sink, Redirect):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(
                s.config["target"]["ca_file"],
                str(Path(s.config["target"]["ca_file"]).with_name("server.key")),
            )
            httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
            servers.append(httpd)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            threads.append(thread)
            thread.start()
        http = Http(
            "https://127.0.0.1:" + str(servers[1].server_port),
            s.config["target"]["ca_file"],
            s.config["target"]["token_ref"],
        )
        with pytest.raises(SecretsError, match="UNAVAILABLE"):
            http.request("GET", "redirect")
        assert received == []
    finally:
        for httpd in servers:
            httpd.shutdown()
            httpd.server_close()
        for thread in threads:
            thread.join(timeout=2)


@pytest.mark.secrets_live
@pytest.mark.integration
def test_actual_bao_and_postgres_json_rotation(live, monkeypatch):
    if not os.environ.get("MIGRATION_TEST_DSN"):
        pytest.skip("MIGRATION_TEST_DSN required for combined real-product rotation")
    import ipaddress
    import uuid
    from datetime import UTC, datetime, timedelta

    import psycopg
    from migration.secrets.common import json_value
    from migration.secrets.models import SecretValue
    from migration.secrets.rotation import rotate
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    s = live
    options = conninfo_to_dict(os.environ["MIGRATION_TEST_DSN"])
    assert ipaddress.ip_address(options["host"]).is_loopback and options.get("password")
    role = "secret_test_" + uuid.uuid4().hex[:12]
    monkeypatch.setenv("LIVE_PG_ADMIN_PASSWORD", options["password"])
    with psycopg.connect(os.environ["MIGRATION_TEST_DSN"], autocommit=True) as admin:
        system_id = admin.execute(
            "SELECT system_identifier::text FROM pg_control_system()"
        ).fetchone()[0]
        user, db = admin.execute("SELECT current_user, current_database()").fetchone()
        admin.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ).format(sql.Identifier(role), sql.Literal("old"))
        )
        try:
            s.source.values["v2"] = SecretValue(
                b'{"credentials":{"password":"old"},"host":"unchanged"}', "string"
            )
            spec = {
                "product": "postgresql",
                "format": "json",
                "password_pointer": "/credentials/password",
                "postgres": {
                    "host": options["host"],
                    "port": int(options.get("port", 5432)),
                    "dbname": db,
                    "role": role,
                    "admin_user": user,
                    "admin_password_ref": "env:LIVE_PG_ADMIN_PASSWORD",
                    "expected_system_identifier": system_id,
                    "sslmode": "disable",
                    "allow_insecure_local_test": True,
                },
            }
            ready = {
                "schema_version": 1,
                "plan_hash": s.plan["plan_hash"],
                "source_arn": s.plan["rows"][0]["source"]["arn"],
                "operation_id": "live-rotation",
                "prepared_by": "local-product-test",
                "issued_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                "application_prepared": True,
                "source_rotation_paused": True,
            }
            with State(s.state) as copied:
                run(s.config, s.plan, s.prepared, s.source, s.target, copied)
                with State(s.directory / "real-rotation.sqlite3") as journal:
                    result = rotate(
                        s.config,
                        s.plan,
                        s.prepared,
                        s.plan["rows"][0],
                        spec,
                        ready,
                        "live-rotation",
                        s.target,
                        copied,
                        journal,
                    )
                    assert result["rotation_status"] == "verified"
                    assert (
                        rotate(
                            s.config,
                            s.plan,
                            s.prepared,
                            s.plan["rows"][0],
                            spec,
                            None,
                            "live-rotation",
                            s.target,
                            copied,
                            journal,
                            check_only=True,
                        )["rotation_status"]
                        == "verified"
                    )
            assert json_value(s.target.read_value(PATH).data)["host"] == "unchanged"
        finally:
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
