"""PostgreSQL 접속 정보와 클라이언트 도구(psycopg·pg_dump·psql) 호출.

비밀번호는 메모리 bytes 로만 들고 다닌다. psycopg 에는 인자로, pg_dump·psql
에는 환경변수 PGPASSWORD 로 넘기고 명령줄에는 절대 넣지 않는다.

  login = pg_client.PgLogin(endpoint, "migration_admin", password)
  ddl = pg_client.PgTools().dump_schema(login, ("public.orders",))
"""

import dataclasses
import os
import re
import subprocess
from collections.abc import Sequence

import psycopg

# 원가연 config 의 표 이름 규칙: 소문자 schema.table.
_TABLE_NAME = re.compile(r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*")
_STDERR_TAIL = 2000
_VERSION = re.compile(r"\(PostgreSQL\) (\d+)")


@dataclasses.dataclass(frozen=True)
class DbEndpoint:
    """DB 하나의 주소. sslmode 는 libpq 값(교차 클라우드 기본 require)."""

    host: str
    port: int
    database: str
    sslmode: str = "require"


@dataclasses.dataclass(frozen=True)
class PgLogin:
    """한 로그인으로 DB 하나에 붙는 데 필요한 전부. repr 에 비밀번호가 없다."""

    endpoint: DbEndpoint
    user: str
    password: bytes = dataclasses.field(repr=False, compare=False)


class PgToolError(Exception):
    """pg_dump·psql 이 0 이 아닌 코드로 끝났다. 메시지는 도구 stderr 끝부분."""


def split_table(name: str) -> tuple[str, str]:
    """'schema.table' 을 (schema, table) 로 나눈다.

    Raises:
        ValueError: 소문자 schema.table 형식이 아닐 때.
    """
    if not _TABLE_NAME.fullmatch(name):
        raise ValueError(f"표 이름은 소문자 schema.table 이어야 한다: {name}")
    schema, table = name.split(".")
    return schema, table


def connect(login: PgLogin, *, autocommit: bool = False) -> psycopg.Connection:
    """Login 으로 psycopg 연결을 연다(기본은 자동 커밋 꺼짐)."""
    endpoint = login.endpoint
    return psycopg.connect(
        host=endpoint.host,
        port=endpoint.port,
        dbname=endpoint.database,
        user=login.user,
        password=login.password.decode("utf-8"),
        sslmode=endpoint.sslmode,
        connect_timeout=10,
        application_name="crosscloud",
        autocommit=autocommit,
    )


def scalar(conn: psycopg.Connection, query: str, params: tuple) -> object:
    """첫 행 첫 열. 행이 없으면 None."""
    row = conn.execute(query, params).fetchone()
    return None if row is None else row[0]


def find_table(conn: psycopg.Connection, table: str) -> tuple[int, str] | None:
    """일반·분할 표의 (oid, 소유자). 없으면 None.

    to_regclass 와 달리 스키마 USAGE 권한 없이도 이름으로 카탈로그를 찾는다.
    """
    schema, name = split_table(table)
    return conn.execute(
        "SELECT c.oid, pg_get_userbyid(c.relowner) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')",
        (schema, name),
    ).fetchone()


@dataclasses.dataclass(frozen=True)
class PgTools:
    """pg_dump·psql 실행기.

    Attributes:
        command_prefix: 도구 이름 앞에 붙일 명령(예: 컨테이너 안에서 돌리는
            `docker exec -i -e PGPASSWORD -e PGSSLMODE <이름>`). 비우면 PATH 의
            도구를 쓴다. 주 버전 규칙은 require_versions 가 지킨다.
    """

    command_prefix: tuple[str, ...] = ()

    def major_version(self, tool: str) -> int:
        """`<tool> --version` 의 주 버전(예: 16).

        Raises:
            PgToolError: 도구가 실패했거나 출력에서 버전을 못 읽었을 때.
        """
        completed = subprocess.run(
            [*self.command_prefix, tool, "--version"],
            capture_output=True,
            check=False,
        )
        found = _VERSION.search(completed.stdout.decode("utf-8", "replace"))
        if completed.returncode != 0 or found is None:
            raise PgToolError(f"{tool} 버전을 읽지 못했다")
        return int(found.group(1))

    def require_versions(self, source_major: int, target_major: int) -> None:
        """도구 주 버전이 서버와 맞는지 확인한다. 어긋나면 바로 멈춘다.

        pg_dump 는 원본 서버와 같은 주 버전이어야 한다 — 새 pg_dump 의 덤프는
        옛 서버가 모르는 설정(PG17 의 transaction_timeout 등)을 담는다.
        psql 은 대상 서버와 같아야 하고, 원본보다 옛 대상으로는 내리지 않는다.

        Raises:
            PgToolError: 주 버전이 규칙에 어긋날 때.
        """
        dump, psql = self.major_version("pg_dump"), self.major_version("psql")
        if dump != source_major or psql != target_major:
            raise PgToolError(
                f"도구 주 버전이 서버와 다르다: pg_dump {dump}·원본 "
                f"{source_major}, psql {psql}·대상 {target_major}"
            )
        if source_major > target_major:
            raise PgToolError(
                f"원본 주 버전 {source_major} 이 대상 {target_major} 보다 높다"
            )

    def dump_schema(self, login: PgLogin, tables: Sequence[str]) -> bytes:
        """표 정의만(데이터·소유자·권한 없이) 덤프한 SQL 을 메모리로 받는다.

        소유 시퀀스·기본값·제약·인덱스는 표와 함께 나온다. 스키마 자체는
        나오지 않는다(run_script 쪽에서 만든다).
        """
        for name in tables:
            split_table(name)  # 형식이 틀리면 도구를 부르기 전에 멈춘다.
        table_args = [f"--table={name}" for name in tables]
        return self._run(
            "pg_dump",
            login,
            [
                "--schema-only",
                "--no-owner",
                "--no-privileges",
                *table_args,
            ],
            stdin=b"",
        )

    def run_script(self, login: PgLogin, script: bytes) -> None:
        """SQL 을 한 트랜잭션으로 실행한다. 첫 오류에서 멈추고 되돌린다."""
        self._run(
            "psql",
            login,
            [
                "--no-psqlrc",
                "--quiet",
                "--set=ON_ERROR_STOP=1",
                "--single-transaction",
            ],
            stdin=script,
        )

    def _run(
        self, tool: str, login: PgLogin, args: list[str], stdin: bytes
    ) -> bytes:
        endpoint = login.endpoint
        argv = [
            *self.command_prefix,
            tool,
            f"--host={endpoint.host}",
            f"--port={endpoint.port}",
            f"--username={login.user}",
            f"--dbname={endpoint.database}",
            *args,
        ]
        env = {
            **os.environ,
            "PGPASSWORD": login.password.decode("utf-8"),
            "PGSSLMODE": endpoint.sslmode,
            "PGCONNECT_TIMEOUT": "10",
        }
        completed = subprocess.run(
            argv, input=stdin, env=env, capture_output=True, check=False
        )
        if completed.returncode != 0:
            tail = completed.stderr.decode("utf-8", "replace")[-_STDERR_TAIL:]
            raise PgToolError(
                f"{tool} 실패(코드 {completed.returncode}): {tail}"
            )
        return completed.stdout
