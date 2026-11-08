"""psycopg 3 연결 헬퍼. sslmode 기본 require, 접속·문장 타임아웃, 서버 식별."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from typing import Any, Iterable

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


# ---- 기준점(watermark) 범위 조회 ------------------------------------------------
# 마이그레이션 시작 때 원본의 max(created_at) 을 기준점으로 저장하고, 그 뒤 단계는 기준점 이후 행(delta)만 비교한다.
# 행은 INSERT 전용·불변(request_id·payload·created_at 고정, UPDATE/DELETE 없음)이므로 한 번 확인한 집합은 다시 읽지 않는다.

DIGEST_BUCKETS = 256  # request_id(uuid) 앞 두 hex 자리 → 256 버킷. uuid4 라 거의 균등하다.


def scope_clause(since: datetime | None, upto: datetime | None) -> tuple[sql.Composable, tuple[Any, ...]]:
    """`created_at > since AND created_at <= upto` WHERE 절. 둘 다 None 이면 전체."""
    parts: list[sql.Composable] = []
    params: list[Any] = []
    if since is not None:
        parts.append(sql.SQL("created_at > %s")); params.append(since)
    if upto is not None:
        parts.append(sql.SQL("created_at <= %s")); params.append(upto)
    if not parts:
        return sql.SQL(""), ()
    return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(parts), tuple(params)


def max_created_at(conn: psycopg.Connection[Any], qualified: str) -> datetime | None:
    """기준점: 현재 테이블의 max(created_at). 빈 테이블이면 None (= 모든 행이 delta)."""
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT max(created_at) FROM {}").format(table_ident(qualified)))
        row = cur.fetchone()
    value = row[0] if row else None
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def count_rows_in_scope(conn: psycopg.Connection[Any], qualified: str, *, since: datetime | None = None,
                        upto: datetime | None = None) -> int:
    where, params = scope_clause(since, upto)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(table_ident(qualified)) + where, params)
        row = cur.fetchone()
    return int(row[0]) if row else 0


def latest_ids_in_scope(conn: psycopg.Connection[Any], qualified: str, n: int, *,
                        since: datetime | None = None) -> list[str]:
    where, params = scope_clause(since, None)
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT request_id FROM {}").format(table_ident(qualified)) + where
                    + sql.SQL(" ORDER BY created_at DESC, request_no DESC LIMIT %s"), (*params, n))
        return [str(r[0]) for r in cur.fetchall()]


def bucket_digests(conn: psycopg.Connection[Any], qualified: str, *, since: datetime | None = None,
                   upto: datetime | None = None) -> dict[str, tuple[int, str]]:
    """버킷별 (건수, digest). 행을 전송하지 않고 서버에서 계산한다.

    행마다 모든 필드를 이어 붙인 md5(32자)를 먼저 만들고, 그 지문들을 request_id 순으로 이어 붙여 다시 md5 를 낸다.
    서버가 집계 중 들고 있는 것은 행 수 × 32바이트뿐이라 payload 크기와 무관하다(행 원문을 이어 붙이면 표 전체 크기만큼 든다).
    정렬은 uuid 타입(바이트 순)으로 해 collation 차이에 영향받지 않고, created_at 은 UTC 마이크로초 문자열로 고정한다.
    """
    where, params = scope_clause(since, upto)
    row_hash = ("md5(request_id::text || chr(31) || run_id || chr(31) || phase || chr(31) || request_no::text "
                "|| chr(31) || payload || chr(31) || to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US+00:00'))")
    query = sql.SQL(
        "SELECT substr(request_id::text, 1, 2) AS bucket, count(*), "
        "md5(string_agg(" + row_hash + ", '' ORDER BY request_id)) FROM {}"
    ).format(table_ident(qualified)) + where + sql.SQL(" GROUP BY 1")
    with conn.cursor() as cur:
        cur.execute(query, params)
        return {str(b): (int(n), str(d)) for b, n, d in cur.fetchall()}


def missing_ids(conn: psycopg.Connection[Any], qualified: str, ids: Iterable[str], batch: int = 5000) -> set[str]:
    """ids 중 테이블에 없는 request_id. batch 개씩 `= ANY(uuid[])` 로 묻고 빠진 것만 모은다. 행도, ids 전체도 메모리에 두지 않는다."""
    missing: set[str] = set()
    it = iter(ids)
    while True:
        chunk = list(islice(it, batch))
        if not chunk:
            return missing
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT request_id::text FROM {} WHERE request_id = ANY(%s::uuid[])")
                        .format(table_ident(qualified)), (chunk,))
            found = {str(r[0]) for r in cur.fetchall()}
        missing.update(set(chunk) - found)


def is_transient(exc: BaseException) -> bool:
    """재시도해도 되는 연결 계열 오류인지."""
    return isinstance(exc, TRANSIENT_ERRORS)


def is_insufficient_privilege(exc: BaseException) -> bool:
    return isinstance(exc, psycopg.errors.InsufficientPrivilege)
