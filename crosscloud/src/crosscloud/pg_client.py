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


def connect(login: PgLogin) -> psycopg.Connection:
    """Login 으로 psycopg 연결을 연다(자동 커밋 꺼짐)."""
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
    )


@dataclasses.dataclass(frozen=True)
class PgTools:
    """pg_dump·psql 실행기.

    Attributes:
        command_prefix: 도구 이름 앞에 붙일 명령(예: 컨테이너 안에서 돌리는
            `docker exec -i -e PGPASSWORD -e PGSSLMODE <이름>`). 비우면 PATH 의
            도구를 쓴다. pg_dump 버전은 서버 주 버전 이상이어야 한다.
    """

    command_prefix: tuple[str, ...] = ()

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
