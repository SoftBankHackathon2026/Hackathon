"""리허설 DB 준비 — 원본 시험 데이터 씨앗, 관리 사용자 로그인, readiness.

비밀번호는 메모리 bytes 로만 다루고 Secret 이 정본이다. DB 에는 SCRAM 검증자만
보낸다(datapath 와 같다). 결과·예외 메시지에 값을 싣지 않는다.
"""

import json
import secrets
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg import sql

from crosscloud import datapath, db_readiness, handoff, pg_client
from crosscloud.rehearsal import config
from crosscloud.secret_transfer import store

# sample-app packages/request-store/schema.sql(363eeb9) 그대로.
SEED_TABLE = "public.migration_demo"
_SEED_SCHEMA = """
CREATE TABLE IF NOT EXISTS public.migration_demo (
    request_id uuid PRIMARY KEY,
    run_id text NOT NULL,
    phase text NOT NULL,
    request_no bigint NOT NULL,
    payload text NOT NULL,
    created_at timestamptz NOT NULL
)
"""
_SEED_ROWS = """
INSERT INTO public.migration_demo
    (request_id, run_id, phase, request_no, payload, created_at)
SELECT gen_random_uuid(), 'ccx_seed', 'seed', n, 'rehearsal seed', now()
FROM generate_series(1, %s) AS n
WHERE NOT EXISTS (SELECT 1 FROM public.migration_demo)
"""
_PASSWORD_BYTES = 32
_SQL_ADMIN = "https://sqladmin.googleapis.com/v1"
_OPERATION_POLL_SECONDS = 3.0
_OPERATION_DEADLINE_SECONDS = 300.0


class PrepError(RuntimeError):
    """준비를 이어 갈 수 없다. 메시지에 비밀값이 없다."""


def seed_source(
    master: pg_client.PgLogin,
    cfg: config.RehearsalConfig,
    secret_store: store.SecretStore,
    url_secret_id: str,
) -> int:
    """원본 DB 에 앱 표·앱 로그인·시험 행과 앱 접속 URL Secret 을 만든다.

    다시 돌려도 같은 결과다. URL Secret 에 값이 있으면 앱 비밀번호를 바꾸지
    않는다(그 값이 정본). 없으면 새 비밀번호를 DB 에 먼저 넣고 Secret 에 쓴다.

    Args:
        master: 원본 DB(cfg.source_database)에 붙은 관리 사용자.
        cfg: 리허설 설정. 표는 sample-app 표(SEED_TABLE)만 받는다.
        secret_store: 원본 클라우드 Secret 저장소.
        url_secret_id: 원본 앱 DATABASE_URL Secret(프로필 A).

    Returns:
        이번에 넣은 시험 행 수(이미 있으면 0).

    Raises:
        PrepError: 표가 sample-app 표가 아닐 때.
    """
    if cfg.table != SEED_TABLE:
        raise PrepError(f"씨앗은 {SEED_TABLE} 만 만든다")
    has_url = secret_store.read_current(url_secret_id) is not None
    password = None if has_url else secrets.token_urlsafe(_PASSWORD_BYTES)
    with pg_client.connect(master) as conn, conn.transaction():
        conn.execute(_SEED_SCHEMA)
        if password is not None:
            _set_login(conn, cfg.app_user, password.encode())
        conn.execute(
            sql.SQL("GRANT SELECT, INSERT ON {} TO {}").format(
                sql.Identifier(*pg_client.split_table(SEED_TABLE)),
                sql.Identifier(cfg.app_user),
            )
        )
        inserted = conn.execute(_SEED_ROWS, (cfg.seed_rows,)).rowcount
    if password is not None:
        url = _app_url(cfg.app_user, password, master.endpoint)
        secret_store.add_version(
            url_secret_id, url, store.version_token(cfg.run, url_secret_id)
        )
    return inserted


def ensure_bootstrap_password(
    secret_store: store.SecretStore,
    secret_id: str,
    set_password: Callable[[bytes], None],
    run_id: str,
) -> bytes:
    """Secret 에 관리 사용자 비밀번호가 없으면 만들어 DB 에 넣고 Secret 에 쓴다.

    DB 에 먼저 넣는다. Secret 쓰기가 실패하면 다음 실행이 새 값으로 다시
    넣으므로 Secret 과 DB 가 어긋난 채 남지 않는다.
    """
    current = secret_store.read_current(secret_id)
    if current is not None:
        return current
    password = secrets.token_urlsafe(_PASSWORD_BYTES).encode()
    set_password(password)
    secret_store.add_version(
        secret_id, password, store.version_token(run_id, secret_id)
    )
    return password


def rds_master(
    secret_value: bytes, endpoint: pg_client.DbEndpoint
) -> pg_client.PgLogin:
    """RDS 가 만든 master Secret(JSON username·password)으로 로그인을 만든다."""
    try:
        document = json.loads(secret_value)
        return pg_client.PgLogin(
            endpoint,
            str(document["username"]),
            str(document["password"]).encode(),
        )
    except (ValueError, KeyError, TypeError):
        raise PrepError("RDS master Secret 형식이 예상과 다르다") from None


def cloud_sql_password_setter(
    session: Any, project: str, instance: str
) -> Callable[[bytes], None]:
    """Cloud SQL postgres 비밀번호를 Admin API 로 바꾸는 함수를 만든다.

    Args:
        session: google.auth.transport.requests.AuthorizedSession.
        project: GCP 프로젝트.
        instance: Cloud SQL 인스턴스 이름.
    """
    base = f"{_SQL_ADMIN}/projects/{project}"

    def set_password(password: bytes) -> None:
        response = session.put(
            f"{base}/instances/{instance}/users",
            params={"name": "postgres"},
            json={"name": "postgres", "password": password.decode()},
            timeout=30,
        )
        operation = _json_or_fail(response, "users.update")
        _wait_operation(session, base, operation["name"])

    return set_password


def collect_readiness(
    source_master: pg_client.PgLogin,
    target_master: pg_client.PgLogin,
    plans: tuple[datapath.PrincipalPlan, datapath.PrincipalPlan],
    target_secrets: store.SecretStore,
) -> tuple[handoff.ReadinessItem, ...]:
    """render-handoff 의 readiness 네 항목을 DB·Secret 을 읽어 판정한다.

    Args:
        source_master: 원본 DB 에 붙은 관리 사용자.
        target_master: 대상 DB 에 붙은 관리 사용자.
        plans: (원본, 대상) 준비 계획.
        target_secrets: 대상 클라우드 Secret 저장소.
    """
    source_plan, target_plan = plans
    with pg_client.connect(source_master) as conn:
        wal_level = str(pg_client.scalar(conn, "SHOW wal_level", ()))
        problems = list(db_readiness.check_db_principals(conn, source_plan))
    with pg_client.connect(target_master) as conn:
        problems += db_readiness.check_db_principals(conn, target_plan)
    nonempty = _nonempty_tables(target_plan, target_secrets)
    profile = target_secrets.read_current(target_plan.app_profile_secret_id)
    return (
        _item(
            "replication_flags",
            wal_level == "logical",
            f"wal_level={wal_level}",
        ),
        _item("migration_logins", not problems, "; ".join(problems) or "ok"),
        _item("target_schema_empty", not nonempty, ", ".join(nonempty) or "ok"),
        _item(
            "profile_secrets",
            profile is not None,
            f"{target_plan.app_profile_secret_id} 값 있음"
            if profile is not None
            else f"{target_plan.app_profile_secret_id} 값 없음",
        ),
    )


def readiness_json(items: tuple[handoff.ReadinessItem, ...]) -> str:
    """render-handoff --readiness 파일 내용."""
    return json.dumps(
        [{"name": x.name, "ready": x.ready, "detail": x.detail} for x in items],
        ensure_ascii=False,
        indent=2,
    )


def _nonempty_tables(
    plan: datapath.PrincipalPlan, target_secrets: store.SecretStore
) -> list[str]:
    # 표는 대상 admin 소유다(datapath). 관리 사용자에게 SELECT 가 없을 수 있다.
    admin = plan.login("admin")
    password = target_secrets.read_current(admin.secret_id)
    if password is None:
        return list(plan.tables)
    login = pg_client.PgLogin(plan.endpoint, admin.name, password)
    found = []
    with pg_client.connect(login) as conn:
        for table in plan.tables:
            exists = pg_client.find_table(conn, table) is not None
            if (
                not exists
                or conn.execute(
                    sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(
                        sql.Identifier(*pg_client.split_table(table))
                    )
                ).fetchone()[0]
            ):
                found.append(table)
    return found


def _item(name: str, ready: bool, detail: str) -> handoff.ReadinessItem:
    return handoff.ReadinessItem(name, ready, detail)  # type: ignore[arg-type]


def _set_login(conn: psycopg.Connection, user: str, password: bytes) -> None:
    verifier = conn.pgconn.encrypt_password(
        password, user.encode(), b"scram-sha-256"
    ).decode()
    exists = pg_client.scalar(
        conn, "SELECT 1 FROM pg_roles WHERE rolname = %s", (user,)
    )
    verb = "ALTER" if exists else "CREATE"
    conn.execute(
        sql.SQL(verb + " ROLE {} WITH LOGIN PASSWORD {}").format(
            sql.Identifier(user), sql.Literal(verifier)
        )
    )


def _app_url(user: str, password: str, endpoint: pg_client.DbEndpoint) -> bytes:
    # datapath._write_profile 과 같은 꼴. TLS 는 앱 env(PGSSLMODE)로 준다.
    quote = urllib.parse.quote
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{endpoint.host}:{endpoint.port}/{quote(endpoint.database, safe='')}"
    ).encode()


def _json_or_fail(response: Any, what: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise PrepError(f"Cloud SQL {what} 실패: HTTP {response.status_code}")
    return response.json()


def _wait_operation(session: Any, base: str, name: str) -> None:
    deadline = time.monotonic() + _OPERATION_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        operation = _json_or_fail(
            session.get(f"{base}/operations/{name}", timeout=30),
            "operations.get",
        )
        if operation.get("status") == "DONE":
            if operation.get("error"):
                raise PrepError(f"Cloud SQL 작업 {name} 실패")
            return
        time.sleep(_OPERATION_POLL_SECONDS)
    raise PrepError(f"Cloud SQL 작업 {name} 대기 초과")
