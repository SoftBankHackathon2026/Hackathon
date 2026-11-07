"""A narrow PostgreSQL password target for the recoverable rotation coordinator.

Passwords are supplied in memory, never in SQL/config/state/errors. libpq builds a
SCRAM verifier locally; only that verifier is sent in the ALTER ROLE statement.
No arbitrary service, group role, database, grant or infrastructure is modified.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from ..models import MigrationError, SecretValue

_RESERVED = frozenset(
    {
        "postgres",
        "rdsadmin",
        "rds_superuser",
        "cloudsqlsuperuser",
        "cloudsqladmin",
        "azure_pg_admin",
        "azure_superuser",
    }
)


def _silence_logs():
    logger = logging.getLogger("psycopg")
    logger.handlers[:] = [logging.NullHandler()]
    logger.propagate = False
    logger.disabled = True
    logger.setLevel(logging.CRITICAL + 1)
    for name, child in tuple(logging.Logger.manager.loggerDict.items()):
        if name.startswith("psycopg.") and isinstance(child, logging.Logger):
            child.handlers.clear()
            child.disabled = True
            child.setLevel(logging.CRITICAL + 1)


def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # An explicit numeric address is required for unencrypted local testing;
        # DNS/hosts-file names such as localhost are not sufficient evidence.
        return False


@dataclass(frozen=True)
class PostgresSpec:
    host: str
    port: int
    dbname: str
    role: str
    admin_user: str
    admin_password_ref: str = field(repr=False)
    expected_system_identifier: str
    sslrootcert: str | None = None
    sslmode: str = "verify-full"
    allow_insecure_local_test: bool = False

    def __post_init__(self):
        fields = (
            self.host,
            self.dbname,
            self.role,
            self.admin_user,
            self.admin_password_ref,
            self.expected_system_identifier,
        )
        if any(
            type(v) is not str or not v or len(v) > 1024 or any(ord(c) < 32 for c in v)
            for v in fields
        ):
            raise MigrationError("invalid_config")
        if (
            type(self.port) is not int
            or not 1 <= self.port <= 65535
            or type(self.allow_insecure_local_test) is not bool
            or not re.fullmatch(r"[0-9]+", self.expected_system_identifier)
        ):
            raise MigrationError("invalid_config")
        # Simple identifiers keep this integration deliberately narrow and reviewable.
        if any(
            not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", v)
            for v in (self.dbname, self.role, self.admin_user)
        ):
            raise MigrationError("invalid_config")
        if (
            self.role == self.admin_user
            or self.role.lower() in _RESERVED
            or self.role.lower().startswith("pg_")
        ):
            raise MigrationError("blocked")
        if not (
            re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", self.admin_password_ref)
            or self.admin_password_ref.startswith("file:/")
        ):
            raise MigrationError("invalid_config")
        if self.sslmode == "disable":
            if not self.allow_insecure_local_test or not _loopback(self.host):
                raise MigrationError("invalid_config")
        elif self.sslmode != "verify-full":
            raise MigrationError("invalid_config")
        if self.sslrootcert is not None and (
            not isinstance(self.sslrootcert, str)
            or not Path(self.sslrootcert).is_absolute()
            or "\x00" in self.sslrootcert
        ):
            raise MigrationError("invalid_config")


def _credential(ref):
    try:
        if ref.startswith("env:"):
            password = os.environ[ref[4:]]
        else:
            path = Path(ref[5:])
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_mode & 0o077
                    or info.st_uid != os.getuid()
                ):
                    raise ValueError()
                password = stream.read(4097)
        if not password or len(password.encode("utf-8")) > 4096 or "\x00" in password:
            raise ValueError()
        return password
    except Exception:
        raise MigrationError("authentication_failed") from None


def _password(value):
    if (
        type(value) is not SecretValue
        or value.kind != "string"
        or not 1 <= len(value.data) <= 4096
        or b"\x00" in value.data
    ):
        raise MigrationError("invalid_state")
    try:
        return value.data.decode("utf-8")
    except UnicodeDecodeError:
        raise MigrationError("invalid_state") from None


class PostgresTarget:
    def __init__(self, spec: PostgresSpec, connect=None):
        if type(spec) is not PostgresSpec:
            raise MigrationError("invalid_config")
        self.spec = spec
        _silence_logs()
        if connect is None:
            try:
                import psycopg

                connect = psycopg.connect
            except ImportError:
                raise MigrationError("dependency_missing") from None
        self._connect = connect

    def __repr__(self):
        return "<PostgresTarget [credentials REDACTED]>"

    def preflight(self):
        """Read-only identity, account and password mechanism checks."""
        try:
            with self._open(
                self.spec.admin_user, _credential(self.spec.admin_password_ref)
            ) as conn:
                self._check_admin(conn)
        except MigrationError:
            raise
        except Exception:
            raise MigrationError("operation_failed") from None

    def _open(self, user, password):
        _silence_logs()
        spec = self.spec
        kwargs = dict(
            host=spec.host,
            port=spec.port,
            dbname=spec.dbname,
            user=user,
            password=password,
            sslmode=spec.sslmode,
            connect_timeout=10,
            autocommit=True,
            require_auth="scram-sha-256",
            options="-c search_path=pg_catalog -c statement_timeout=10000 -c lock_timeout=5000",
        )
        if spec.sslrootcert:
            kwargs["sslrootcert"] = spec.sslrootcert
        if spec.sslmode == "disable":
            kwargs["hostaddr"] = (
                spec.host
            )  # Prevent PGHOSTADDR from escaping the local test scope.
        try:
            conn = self._connect(**kwargs)
            # A caller might have enabled libpq wire tracing on an injected connection.
            conn.pgconn.untrace()
            return conn
        except Exception:
            raise MigrationError("authentication_failed") from None

    def _check_admin(self, conn):
        spec = self.spec
        with conn.cursor() as cursor:
            cursor.execute("SELECT current_user, current_database()")
            if cursor.fetchone() != (spec.admin_user, spec.dbname):
                raise MigrationError("identity_mismatch")
            cursor.execute("SELECT system_identifier::text FROM pg_control_system()")
            row = cursor.fetchone()
            if row != (spec.expected_system_identifier,):
                raise MigrationError("identity_mismatch")
            cursor.execute("SHOW password_encryption")
            if cursor.fetchone() != ("scram-sha-256",):
                raise MigrationError("blocked")
            cursor.execute(
                "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = %s",
                (spec.role,),
            )
            role = cursor.fetchone()
            if role != (True, False, False, False, False, False):
                raise MigrationError("blocked")
            # Do not rotate a seemingly ordinary login that inherits privileged groups.
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname <> %s "
                "AND pg_has_role(%s, oid, 'MEMBER') AND "
                "(rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls "
                "OR left(rolname, 3) = 'pg_' OR rolname IN "
                "('rds_superuser','cloudsqlsuperuser','azure_pg_admin','azure_superuser')))",
                (spec.role, spec.role),
            )
            if cursor.fetchone() != (False,):
                raise MigrationError("blocked")

    def ensure(self, value: SecretValue, rotation_id: str) -> None:
        _password(value)
        if type(rotation_id) is not str or not re.fullmatch(
            r"[A-Za-z0-9_-]{8,128}", rotation_id
        ):
            raise MigrationError("invalid_state")
        try:
            from psycopg import pq, sql

            with self._open(
                self.spec.admin_user, _credential(self.spec.admin_password_ref)
            ) as conn:
                self._check_admin(conn)
                # Crash recovery installs the same candidate, never generates a new one.
                if self.verify(value):
                    return
                verifier = conn.pgconn.encrypt_password(
                    value.data, self.spec.role.encode("utf-8"), b"scram-sha-256"
                )
                if not isinstance(verifier, bytes) or not verifier.startswith(
                    b"SCRAM-SHA-256$"
                ):
                    raise MigrationError("verification_failed")
                with conn.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                            sql.Identifier(self.spec.role),
                            sql.Literal(verifier.decode("ascii")),
                        )
                    )
                    if (
                        cursor.pgresult is None
                        or cursor.pgresult.status != pq.ExecStatus.COMMAND_OK
                    ):
                        raise MigrationError("verification_failed")
                if self.verify(value) is not True:
                    raise MigrationError("verification_failed")
        except MigrationError:
            raise
        except Exception:
            # Remote diagnostics, connection strings, values and SCRAM verifiers are discarded.
            raise MigrationError("operation_failed") from None

    def verify(self, value: SecretValue) -> bool:
        password = _password(value)
        try:
            with self._open(self.spec.role, password) as conn:
                if conn.pgconn.used_password is not True:
                    return False
                with conn.cursor() as cursor:
                    cursor.execute("SELECT current_user, current_database(), 1")
                    return cursor.fetchone() == (self.spec.role, self.spec.dbname, 1)
        except Exception:
            return False
