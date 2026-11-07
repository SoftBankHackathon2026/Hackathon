"""Narrow PostgreSQL credential rotation: local doubles and opt-in isolated PG test."""

from __future__ import annotations

import logging
import os
import uuid
from types import SimpleNamespace

import pytest
from migration.secrets.models import MigrationError, SecretValue
from migration.secrets.operations.postgres import PostgresSpec, PostgresTarget

CANARY = b"PRIVATE-ROTATION-PASSWORD-CANARY"


def spec(**changes):
    values = dict(
        host="127.0.0.1",
        port=5432,
        dbname="rotation_test",
        role="app_rotation",
        admin_user="rotation_admin",
        admin_password_ref="env:ROTATION_TEST_ADMIN_PASSWORD",
        expected_system_identifier="123456789",
        sslmode="disable",
        allow_insecure_local_test=True,
    )
    values.update(changes)
    return PostgresSpec(**values)


class PgDouble:
    def __init__(self):
        self.password = "old"
        self.system_id = "123456789"
        self.encryption = "scram-sha-256"
        self.role = (True, False, False, False, False, False)
        self.privileged_group = False
        self.status = 1  # libpq COMMAND_OK, not merely a truthy success guess.
        self.used_password = True
        self.alter_count = 0
        self.statements = []
        self.connection_kwargs = []
        self.crash_after_change = False
        self.wrong_app_identity = False
        self.raw_error = False

    def connect(self, **kwargs):
        self.connection_kwargs.append(kwargs)
        logging.getLogger("psycopg.connection.new_child").debug(
            "PRIVATE-ROTATION-PASSWORD-CANARY %r", kwargs
        )
        if self.raw_error:
            raise RuntimeError(
                "PRIVATE-ROTATION-PASSWORD-CANARY connection password=raw"
            )
        if kwargs["user"] == "app_rotation" and kwargs["password"] != self.password:
            raise RuntimeError("PRIVATE-ROTATION-PASSWORD-CANARY bad password")
        return PgConnection(self, kwargs["user"])


class PgConnection:
    def __init__(self, server, user):
        self.server = server
        self.user = user
        self.pgconn = SimpleNamespace(
            untrace=lambda: None,
            used_password=server.used_password,
            encrypt_password=self.encrypt_password,
        )

    def encrypt_password(self, password, user, algorithm):
        assert user == b"app_rotation"
        assert algorithm == b"scram-sha-256"
        self.server.candidate = password.decode()
        return b"SCRAM-SHA-256$4096:LOCAL-TEST-VERIFIER"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return PgCursor(self)


class PgCursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None
        self.pgresult = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        text = query if isinstance(query, str) else query.as_string(None)
        server = self.conn.server
        server.statements.append((text, params))
        if text == "SELECT current_user, current_database()":
            self.row = (self.conn.user, "rotation_test")
        elif text == "SELECT system_identifier::text FROM pg_control_system()":
            self.row = (server.system_id,)
        elif text == "SHOW password_encryption":
            self.row = (server.encryption,)
        elif text.startswith("SELECT rolcanlogin"):
            self.row = server.role
        elif text.startswith("SELECT EXISTS"):
            self.row = (server.privileged_group,)
        elif text == "SELECT current_user, current_database(), 1":
            self.row = (
                "unrelated_role" if server.wrong_app_identity else self.conn.user,
                "rotation_test",
                1,
            )
        elif text.startswith("ALTER ROLE"):
            assert "PRIVATE-ROTATION" not in text
            assert "SCRAM-SHA-256$" in text
            server.alter_count += 1
            if server.status == 1:
                server.password = server.candidate
            if server.crash_after_change:
                server.crash_after_change = False
                raise RuntimeError(
                    "PRIVATE-ROTATION-PASSWORD-CANARY lost acknowledgement"
                )
            self.pgresult = SimpleNamespace(status=server.status)
        else:
            raise AssertionError(text)
        return self

    def fetchone(self):
        return self.row


@pytest.fixture
def target(monkeypatch):
    monkeypatch.setenv("ROTATION_TEST_ADMIN_PASSWORD", "PRIVATE-ADMIN-CANARY")
    server = PgDouble()
    return server, PostgresTarget(spec(), connect=server.connect)


def test_postgres_password_install_verify_and_idempotent_retry(target, caplog):
    server, service = target
    caplog.set_level(logging.DEBUG)
    value = SecretValue(CANARY, "string")
    assert service.verify(value) is False
    service.ensure(value, "rotation-one")
    assert service.verify(value) is True
    service.ensure(value, "rotation-one")
    assert server.alter_count == 1
    assert all(CANARY.decode() not in statement for statement, _ in server.statements)
    assert CANARY.decode() not in caplog.text
    assert "PRIVATE-ADMIN" not in repr(service)
    assert all(
        k["require_auth"] == "scram-sha-256" and k["hostaddr"] == "127.0.0.1"
        for k in server.connection_kwargs
    )


def test_postgres_change_acknowledgement_lost_reuses_candidate(target):
    server, service = target
    server.crash_after_change = True
    candidate = SecretValue(CANARY, "string")
    with pytest.raises(MigrationError, match="operation_failed") as error:
        service.ensure(candidate, "rotation-one")
    assert "PRIVATE" not in str(error.value)
    assert server.password == CANARY.decode()
    service.ensure(candidate, "rotation-one")
    assert service.verify(candidate)
    assert server.alter_count == 1


@pytest.mark.parametrize(
    "attribute,value,code",
    [
        ("system_id", "999999", "identity_mismatch"),
        ("encryption", "md5", "blocked"),
        ("role", (True, True, False, False, False, False), "blocked"),
        ("role", (False, False, False, False, False, False), "blocked"),
        ("role", None, "blocked"),
        ("privileged_group", True, "blocked"),
        (
            "status",
            7,
            "verification_failed",
        ),  # libpq FATAL_ERROR must never count as success.
    ],
)
def test_postgres_unsafe_target_or_failed_command_blocks(
    target, attribute, value, code
):
    server, service = target
    setattr(server, attribute, value)
    with pytest.raises(MigrationError, match=code):
        service.ensure(SecretValue(CANARY, "string"), "rotation-one")
    if attribute != "status":
        assert server.alter_count == 0


def test_postgres_verify_rejects_trust_auth_and_wrong_identity(target):
    server, service = target
    server.password = CANARY.decode()
    server.used_password = False
    assert service.verify(SecretValue(CANARY, "string")) is False
    server.used_password = True
    server.wrong_app_identity = True
    assert service.verify(SecretValue(CANARY, "string")) is False


@pytest.mark.parametrize(
    "changes",
    [
        {"sslmode": "disable", "host": "192.0.2.1"},
        {"sslmode": "disable", "host": "localhost"},
        {"sslmode": "disable", "allow_insecure_local_test": False},
        {"sslmode": "require"},
        {"role": "postgres"},
        {"role": "pg_read_all_data"},
        {"role": "rotation_admin"},
        {"role": "app;DROP_ROLE"},
        {"admin_password_ref": "raw-secret"},
        {"expected_system_identifier": "unknown"},
        {"port": True},
    ],
)
def test_postgres_spec_rejects_insecure_or_broad_configuration(changes):
    with pytest.raises(MigrationError):
        spec(**changes)


def test_postgres_tls_defaults_are_full_verification():
    secure = PostgresSpec(
        "db.example", 5432, "orders", "app", "admin", "env:ADMIN", "123"
    )
    assert secure.sslmode == "verify-full"
    assert secure.allow_insecure_local_test is False


def test_postgres_admin_file_requires_private_owned_regular_file(tmp_path, monkeypatch):
    path = tmp_path / "credential"
    path.write_text("PRIVATE-ADMIN-CANARY")
    path.chmod(0o644)
    server = PgDouble()
    service = PostgresTarget(
        spec(admin_password_ref="file:" + str(path)), server.connect
    )
    with pytest.raises(MigrationError, match="authentication_failed"):
        service.ensure(SecretValue(CANARY, "string"), "rotation-one")
    assert not server.connection_kwargs
    path.chmod(0o600)
    service.ensure(SecretValue(CANARY, "string"), "rotation-one")
    assert service.verify(SecretValue(CANARY, "string"))
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(MigrationError, match="authentication_failed"):
        PostgresTarget(
            spec(admin_password_ref="file:" + str(link)), server.connect
        ).ensure(SecretValue(CANARY, "string"), "rotation-two")


@pytest.mark.parametrize(
    "value",
    [
        SecretValue(b"raw", "binary"),
        SecretValue(b"\x00", "string"),
        SecretValue(b"\xff", "string"),
        SecretValue(b"", "string"),
    ],
)
def test_postgres_password_shape_is_not_guessed(target, value):
    _, service = target
    with pytest.raises(MigrationError, match="invalid_state"):
        service.ensure(value, "rotation-one")


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("MIGRATION_TEST_DSN"),
    reason="MIGRATION_TEST_DSN missing: live PostgreSQL rotation not tested",
)
def test_postgres_live_rotation_with_temporary_role_and_database(monkeypatch):
    """Only the explicitly supplied loopback test PostgreSQL is changed, then cleaned."""
    import ipaddress

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    options = conninfo_to_dict(os.environ["MIGRATION_TEST_DSN"])
    host = options.get("host", "")
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback or not options.get("password"):
        pytest.skip(
            "rotation integration requires an explicit numeric loopback test server and password"
        )
    suffix = uuid.uuid4().hex[:12]
    role, db = "rot_app_" + suffix, "rot_db_" + suffix
    monkeypatch.setenv("ROTATION_INTEGRATION_ADMIN_PASSWORD", options["password"])
    with psycopg.connect(os.environ["MIGRATION_TEST_DSN"], autocommit=True) as admin:
        system_id = admin.execute(
            "SELECT system_identifier::text FROM pg_control_system()"
        ).fetchone()[0]
        admin_user = admin.execute("SELECT current_user").fetchone()[0]
        admin.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            ).format(sql.Identifier(role))
        )
        created_db = False
        try:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db)))
            created_db = True
            local_spec = PostgresSpec(
                host=host,
                port=int(options.get("port", "5432")),
                dbname=db,
                role=role,
                admin_user=admin_user,
                admin_password_ref="env:ROTATION_INTEGRATION_ADMIN_PASSWORD",
                expected_system_identifier=system_id,
                sslmode="disable",
                allow_insecure_local_test=True,
            )
            service = PostgresTarget(local_spec)
            candidate = SecretValue(
                ("integration-" + uuid.uuid4().hex).encode(), "string"
            )
            assert service.verify(candidate) is False
            service.ensure(candidate, "integration-" + suffix)
            assert service.verify(candidate) is True
            service.ensure(candidate, "integration-" + suffix)
            assert service.verify(candidate) is True
            assert (
                service.verify(SecretValue(b"wrong-test-password", "string")) is False
            )
            stored = admin.execute(
                "SELECT rolpassword FROM pg_authid WHERE rolname=%s", (role,)
            ).fetchone()[0]
            assert stored.startswith("SCRAM-SHA-256$")
            assert candidate.data.decode() not in stored
        finally:
            if created_db:
                admin.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db))
                )
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_postgres_admin_file_ignores_trailing_newline(tmp_path):
    path = tmp_path / "credential"
    path.write_text("PRIVATE-ADMIN-CANARY\n")
    path.chmod(0o600)
    server = PgDouble()
    PostgresTarget(
        spec(admin_password_ref="file:" + str(path)), server.connect
    ).preflight()
    assert any(
        kwargs.get("password") == "PRIVATE-ADMIN-CANARY"
        for kwargs in server.connection_kwargs
    )
