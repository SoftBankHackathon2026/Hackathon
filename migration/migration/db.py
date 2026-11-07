"""psycopg 3 연결 헬퍼. sslmode 기본 require, 접속·문장 타임아웃, 서버 식별."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row
from psycopg_pool import ConnectionPool

from .config import ConnectionSpec, redact

log = logging.getLogger(__name__)

TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    psycopg.OperationalError,
    psycopg.InterfaceError,
)


@dataclass(frozen=True)
class ServerIdentity:
    """실제로 붙은 서버가 어디인지 증명하는 값."""

    database: str
    user: str
    server_addr: str | None
    server_port: int | None
    version: str
    is_in_recovery: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "user": self.user,
            "server_addr": self.server_addr,
            "server_port": self.server_port,
            "version": self.version,
            "is_in_recovery": self.is_in_recovery,
        }


def connect(
    spec: ConnectionSpec,
    *,
    autocommit: bool = False,
    statement_timeout_ms: int = 30_000,
) -> psycopg.Connection[Any]:
    """접속하고 statement_timeout 을 세션에 적용한다. 오류 메시지에서 비밀번호를 제거한다."""
    try:
        conn = psycopg.connect(
            spec.conninfo(),
            password=spec.password,
            autocommit=autocommit,
            row_factory=tuple_row,
        )
    except psycopg.Error as exc:
        raise psycopg.OperationalError(redact(f"접속 실패 {spec}: {exc}")) from None
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SET statement_timeout = {}").format(sql.Literal(int(statement_timeout_ms))))
        if not autocommit:
            conn.commit()
    except psycopg.Error:
        conn.close()
        raise
    return conn


def connection_pool(spec: ConnectionSpec) -> ConnectionPool:
    """A small, lazy pool for one target/role; transactions finish on return."""
    def configure(conn: psycopg.Connection[Any]) -> None:
        conn.execute("SET statement_timeout = 30000")
        conn.commit()

    return ConnectionPool(
        spec.conninfo(), kwargs={"password": spec.password, "row_factory": tuple_row},
        min_size=1, max_size=2, timeout=10, open=True,
        configure=configure, check=ConnectionPool.check_connection,
    )


def server_identity(conn: psycopg.Connection[Any]) -> ServerIdentity:
    """current_database/user/inet_server_addr 등을 읽는다."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_database(), current_user, host(inet_server_addr()), inet_server_port(), "
            "version(), pg_is_in_recovery()"
        )
        row = cur.fetchone()
    assert row is not None
    return ServerIdentity(
        database=row[0],
        user=row[1],
        server_addr=row[2],
        server_port=row[3],
        version=row[4],
        is_in_recovery=bool(row[5]),
    )


def split_table(qualified: str) -> tuple[str, str]:
    """'public.migration_demo' → ('public', 'migration_demo')."""
    if "." in qualified:
        schema, name = qualified.split(".", 1)
        return schema, name
    return "public", qualified


def table_ident(qualified: str) -> sql.Composed:
    schema, name = split_table(qualified)
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(name))


def table_exists(conn: psycopg.Connection[Any], qualified: str) -> bool:
    schema, name = split_table(qualified)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
            (schema, name),
        )
        return cur.fetchone() is not None


def has_table_privilege(conn: psycopg.Connection[Any], role: str, qualified: str, privilege: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT has_table_privilege(%s, %s, %s)", (role, qualified, privilege))
        row = cur.fetchone()
    return bool(row and row[0])


def count_rows(conn: psycopg.Connection[Any], qualified: str, phase: str | None = None) -> int:
    query = sql.SQL("SELECT count(*) FROM {}").format(table_ident(qualified))
    params: tuple[Any, ...] = ()
    if phase is not None:
        query = query + sql.SQL(" WHERE phase = %s")
        params = (phase,)
    with conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    return int(row[0]) if row else 0


def is_transient(exc: BaseException) -> bool:
    """재시도해도 되는 연결 계열 오류인지."""
    return isinstance(exc, TRANSIENT_ERRORS)


def is_insufficient_privilege(exc: BaseException) -> bool:
    return isinstance(exc, psycopg.errors.InsufficientPrivilege)
