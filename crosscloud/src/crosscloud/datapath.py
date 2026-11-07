"""원가연 전환 도구(migration.cloud)가 쓰는 DB 로그인과 대상 스키마를 준비한다.

원가연 config 의 users 역할(admin·app·cdc·sink·verify)에 맞춰 한쪽 DB 에
로그인을 만든다. 원본은 admin·cdc·verify, 대상은 admin·app·sink·verify 다.

*   admin: 지정 표의 소유자. 원가연이 publication·쓰기 차단 트리거·잠금을
    admin 으로 실행한다. DB CREATE 권한. 대상 admin 은 app 의 LOGIN 을 켜고
    끄도록 CREATEROLE 과 app 의 ADMIN 옵션을 가진다.
*   app(대상): 원본 앱 로그인의 실효 권한(그룹 역할·PUBLIC 에서 상속한 것
    포함)을 표·소유 시퀀스·스키마 단위로 직접 GRANT 한다. 표를 소유하지 않고
    언제나 NOLOGIN 으로 맞추며, 붙어 있던 app 세션은 끊는다. 쓰기 개방
    (/target) 때 open_app_login 이 켠다. 그 전까지 대상에 쓸 수 있는 앱
    로그인이 없다(단일 쓰기 DB).
*   cdc(원본): 복제 권한(RDS 는 rds_replication, Cloud SQL 은 REPLICATION)과
    SELECT. admin 이 publication 소유자를 cdc 로 넘길 수 있게 SET 멤버십.
*   sink(대상): 지정 표 DML. verify: 지정 표 SELECT.

비밀번호는 로그인마다 Secret 하나가 정본이다. 값이 있으면 그 값을, 없으면 새로
만들어 Secret 에 먼저 쓴 뒤 DB 에 넣는다 — 그래서 다시 돌려도 같은 비밀번호다.
DB 에는 클라이언트가 계산한 SCRAM 검증자만 보내 서버 로그에 평문이 남지 않는다.

대상 DB 가 없으면 만든다(job 마다 새 이름 `ccx_<job_id>` — 설계 4.18 (d)).
대상 스키마는 원본에서 `pg_dump --schema-only --no-owner --no-privileges` 로
떠서 대상 admin 으로 복원한다. 도구 주 버전이 서버와 어긋나면 아무것도 바꾸기
전에 멈춘다. 데이터는 넣지 않는다(원가연 PRECHECK 가 빈 대상을 요구한다).

준비가 끝났는지는 db_readiness.check_db_principals 가 카탈로그로 확인한다.
"""

import dataclasses
import secrets
import time
import urllib.parse
from collections.abc import Iterable
from typing import Literal

import psycopg
from psycopg import sql

from crosscloud import model, pg_client
from crosscloud.secret_transfer import store

LoginPurpose = Literal["admin", "app", "cdc", "sink", "verify"]
Side = Literal["source", "target"]

REQUIRED_PURPOSES: dict[Side, frozenset[str]] = {
    "source": frozenset({"admin", "cdc", "verify"}),
    "target": frozenset({"admin", "app", "sink", "verify"}),
}
SINK_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
_TABLE_MIRROR = ("SELECT", "INSERT", "UPDATE", "DELETE")
_SEQUENCE_MIRROR = ("USAGE", "SELECT", "UPDATE")
_PASSWORD_BYTES = 32
_PG16 = 160000
_SESSION_WAIT_SECONDS = 10.0
_SESSION_POLL_SECONDS = 0.2
# PATH 의 pg_dump·psql. PgTools 는 바꿀 수 없는 값이라 기본값으로 공유한다.
_PATH_TOOLS = pg_client.PgTools()


@dataclasses.dataclass(frozen=True)
class DbLogin:
    """원가연 역할 하나의 로그인 이름과 비밀번호 Secret(그 DB 의 클라우드)."""

    purpose: LoginPurpose
    name: str
    secret_id: str


@dataclasses.dataclass(frozen=True)
class PrincipalPlan:
    """DB 한쪽에 만들 로그인.

    Attributes:
        direction: 이전 방향. side 와 함께 이 DB 의 클라우드를 정한다.
        side: 원본(source)인지 대상(target)인지.
        endpoint: 이 DB 의 주소. database 가 DB 이름이다(대상은 job 마다 새
            이름을 줄 수 있고, 없으면 만든다).
        logins: 쪽마다 정해진 역할 전부(원본 admin·cdc·verify, 대상
            admin·app·sink·verify).
        tables: 옮길 표(원가연 application.tables 의 소문자 schema.table).
        source_app_user: 원본 앱 로그인. 대상 app 이 이 로그인의 권한을
            미러하고, 원본 쪽 로그인 이름은 이것과 겹치면 안 된다.
        run_id: 이 실행의 식별자. Secret 쓰기 토큰의 재료다.
        app_profile_secret_id: 대상만. 대상 앱이 쓸 접속 URL 을 담을 Secret.
    """

    direction: model.Direction
    side: Side
    endpoint: pg_client.DbEndpoint
    logins: tuple[DbLogin, ...]
    tables: tuple[str, ...]
    source_app_user: str
    run_id: str
    app_profile_secret_id: str | None = None

    def __post_init__(self) -> None:
        """역할 구성과 이름을 검사한다.

        Raises:
            ValueError: 쪽에 맞는 역할이 정확히 하나씩이 아니거나, 이름이
                겹치거나, 원본 쪽 로그인이 원본 앱 로그인과 같거나, 표 이름
                형식이 틀리거나, 원본에 프로필 Secret 이 있을 때.
        """
        purposes = [login.purpose for login in self.logins]
        if sorted(purposes) != sorted(REQUIRED_PURPOSES[self.side]):
            required = sorted(REQUIRED_PURPOSES[self.side])
            raise ValueError(f"{self.side} 로그인은 {required} 하나씩이다")
        names = {login.name for login in self.logins}
        if len(names) != len(self.logins):
            raise ValueError("로그인 이름이 겹친다")
        if self.side == "source" and self.source_app_user in names:
            raise ValueError("원본 로그인 이름이 원본 앱 로그인과 겹친다")
        if not self.tables:
            raise ValueError("tables 가 비었다")
        for name in self.tables:
            pg_client.split_table(name)
        if self.side == "source" and self.app_profile_secret_id is not None:
            raise ValueError("프로필 Secret 은 대상에만 쓴다")

    def login(self, purpose: LoginPurpose) -> DbLogin:
        """Purpose 역할의 로그인."""
        return next(x for x in self.logins if x.purpose == purpose)

    @property
    def cloud(self) -> model.Cloud:
        """이 DB 가 있는 클라우드."""
        source, target = self.direction.split("_to_")
        return source if self.side == "source" else target  # type: ignore[return-value]


@dataclasses.dataclass(frozen=True)
class PreparedLogin:
    """준비한 로그인 하나. written 은 이번 실행이 새로 쓴 비밀번호 버전."""

    purpose: LoginPurpose
    name: str
    secret_id: str
    written: store.WrittenVersion | None


class PrincipalError(Exception):
    """준비를 이어 갈 수 없는 DB 상태(표가 일부만 있음 등)."""


MirrorKind = Literal["TABLE", "SEQUENCE", "SCHEMA"]
_MIRROR_PRIVILEGES: dict[str, tuple[str, ...]] = {
    "TABLE": _TABLE_MIRROR,
    "SEQUENCE": _SEQUENCE_MIRROR,
    "SCHEMA": ("USAGE",),
}


@dataclasses.dataclass(frozen=True)
class MirroredGrant:
    """원본 app 의 실효 권한 하나 — 대상 app 에 그대로 GRANT 한다.

    원본 서버가 돌려준 값이라 믿지 않는다. 종류·권한은 허용 목록으로 거르고,
    이름은 SQL 에 식별자로만 넣는다.

    Attributes:
        kind: 객체 종류.
        schema: 스키마 이름.
        name: 표·시퀀스 이름. 스키마 권한이면 None.
        privilege: 권한 이름.
    """

    kind: MirrorKind
    schema: str
    name: str | None
    privilege: str

    @classmethod
    def from_row(cls, row: tuple[str, str, str | None, str]) -> "MirroredGrant":
        """질의 행(종류, 스키마, 이름, 권한)을 검사해 만든다.

        Raises:
            PrincipalError: 종류나 권한이 허용 목록 밖일 때.
        """
        kind, schema, name, privilege = row
        if privilege not in _MIRROR_PRIVILEGES.get(kind, ()):
            raise PrincipalError(f"미러할 수 없는 권한: {kind} {privilege}")
        if (name is None) != (kind == "SCHEMA"):
            raise PrincipalError(f"미러할 수 없는 객체: {kind} {schema}")
        return cls(kind, schema, name, privilege)  # type: ignore[arg-type]

    def statement(self, grantee: str) -> sql.Composed:
        """이 권한을 grantee 에게 주는 GRANT 문."""
        target = (
            sql.Identifier(self.schema)
            if self.name is None
            else sql.Identifier(self.schema, self.name)
        )
        # kind·privilege 는 from_row 가 허용 목록으로 거른 상수다.
        return sql.SQL("GRANT {} ON {} {} TO {}").format(
            sql.SQL(self.privilege),
            sql.SQL(self.kind),
            target,
            sql.Identifier(grantee),
        )


def prepare_db_principals(
    plan: PrincipalPlan,
    bootstrap: pg_client.PgLogin,
    secret_store: store.SecretStore,
    schema_source: pg_client.PgLogin | None = None,
    tools: pg_client.PgTools = _PATH_TOOLS,
) -> tuple[PreparedLogin, ...]:
    """Plan 의 로그인을 만들고 권한을 맞춘다. 다시 돌려도 같은 결과다.

    대상은 원가연 PRECHECK 전에만 돌린다 — app 을 NOLOGIN 으로 되돌리고
    세션을 끊으므로, 쓰기 개방 뒤에 돌리면 서비스가 끊긴다.

    Args:
        plan: 만들 로그인과 표.
        bootstrap: 그 DB 서버의 관리 사용자(RDS master·Cloud SQL postgres)
            로그인. 주소는 plan.endpoint 와 같고, DB 는 관리 DB(보통
            postgres)다. CREATEROLE(대상은 CREATEDB 도)이 있어야 한다.
        secret_store: 이 DB 와 같은 클라우드의 Secret 저장소.
        schema_source: 대상일 때만, 필수. 스키마와 앱 권한을 읽을 원본 DB
            로그인(표 SELECT 가 필요하다).
        tools: 대상 스키마 복원에 쓸 pg_dump·psql.

    Returns:
        로그인마다 준비 결과.

    Raises:
        ValueError: 대상인데 schema_source 가 없거나, bootstrap 주소가 plan 과
            다를 때.
        store.UnsafeLoggingError: SDK 로거가 DEBUG 라 비밀값이 남을 수 있을 때.
        PrincipalError: 로그인 이름이 관리 사용자와 겹치거나, 대상 표가 일부만
            있거나, app 세션이 끊기지 않을 때.
        pg_client.PgToolError: 도구 주 버전 불일치·스키마 덤프·복원 실패.
        store.SecretStoreError: 비밀번호 Secret 읽기·쓰기 실패.
    """
    if plan.side == "target" and schema_source is None:
        raise ValueError("대상 준비에는 schema_source 가 필요하다")
    here = (plan.endpoint.host, plan.endpoint.port)
    if (bootstrap.endpoint.host, bootstrap.endpoint.port) != here:
        raise ValueError("bootstrap 주소가 plan.endpoint 와 다르다")
    store.refuse_sdk_debug_logging()
    if plan.side == "target":
        return _prepare_target(
            plan, bootstrap, secret_store, schema_source, tools
        )
    return _prepare_source(plan, bootstrap, secret_store)


def open_app_login(admin: psycopg.Connection, app: str) -> None:
    """대상 app 로그인을 켠다(쓰기 개방 때). admin 은 대상 admin 연결."""
    with admin.transaction():
        admin.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN").format(sql.Identifier(app))
        )


def close_app_login(admin: psycopg.Connection, app: str) -> None:
    """대상 app 로그인을 끄고, 붙어 있던 app 세션이 모두 끊길 때까지 기다린다.

    admin 은 열린 트랜잭션이 없는 대상 admin 연결이다(세션 목록은
    트랜잭션마다 새로 읽어야 한다).

    Raises:
        PrincipalError: 기다려도 app 세션이 남아 있을 때.
    """
    with admin.transaction():
        admin.execute(
            sql.SQL("ALTER ROLE {} WITH NOLOGIN").format(sql.Identifier(app))
        )
    _end_sessions(admin, app)


def _prepare_target(
    plan: PrincipalPlan,
    bootstrap: pg_client.PgLogin,
    secret_store: store.SecretStore,
    source_login: pg_client.PgLogin,
    tools: pg_client.PgTools,
) -> tuple[PreparedLogin, ...]:
    with pg_client.connect(source_login) as source:
        source_major = source.info.server_version // 10000
        mirrored = _app_grants(source, plan.source_app_user, plan.tables)
    with pg_client.connect(bootstrap, autocommit=True) as maintenance:
        tools.require_versions(
            source_major, maintenance.info.server_version // 10000
        )
        _create_database_if_missing(maintenance, plan.endpoint.database)
    master = dataclasses.replace(bootstrap, endpoint=plan.endpoint)
    with pg_client.connect(master) as conn:
        _refuse_reserved_names(conn, plan, ())
    passwords, prepared = _ensure_passwords(plan, secret_store)
    with pg_client.connect(master) as conn:
        _bootstrap_target(conn, plan, passwords)
    admin = pg_client.PgLogin(
        plan.endpoint, plan.login("admin").name, passwords["admin"]
    )
    with pg_client.connect(admin, autocommit=True) as conn:
        _end_sessions(conn, plan.login("app").name)
    _restore_schema(plan, master, admin, source_login, tools)
    with pg_client.connect(admin) as conn, conn.transaction():
        _grant_target(conn, plan, mirrored)
    _write_profile(plan, passwords["app"], secret_store)
    return prepared


def _prepare_source(
    plan: PrincipalPlan,
    bootstrap: pg_client.PgLogin,
    secret_store: store.SecretStore,
) -> tuple[PreparedLogin, ...]:
    master = dataclasses.replace(bootstrap, endpoint=plan.endpoint)
    with pg_client.connect(master) as conn:
        _refuse_reserved_names(conn, plan, (plan.source_app_user,))
        _refuse_app_owned_tables(conn, plan)
    passwords, prepared = _ensure_passwords(plan, secret_store)
    with pg_client.connect(master) as conn:
        _bootstrap_source(conn, plan, passwords)
    admin = pg_client.PgLogin(
        plan.endpoint, plan.login("admin").name, passwords["admin"]
    )
    with pg_client.connect(admin) as conn, conn.transaction():
        for table in plan.tables:
            conn.execute(
                sql.SQL("GRANT SELECT ON {} TO {}, {}").format(
                    _table_ident(table),
                    _ident(plan, "cdc"),
                    _ident(plan, "verify"),
                )
            )
    return prepared


def _refuse_reserved_names(
    conn: psycopg.Connection, plan: PrincipalPlan, extra: tuple[str, ...]
) -> None:
    # 우리가 만들지 않은 역할(관리 사용자·원본 앱)의 비밀번호·속성을 덮지
    # 않는다.
    reserved = {str(pg_client.scalar(conn, "SELECT current_user", ())), *extra}
    clashes = sorted(reserved & {login.name for login in plan.logins})
    if clashes:
        raise PrincipalError(
            "로그인 이름이 관리 사용자·원본 앱과 겹친다: " + ", ".join(clashes)
        )


def _refuse_app_owned_tables(
    conn: psycopg.Connection, plan: PrincipalPlan
) -> None:
    # 원본 app 이 소유하거나 그 소유 역할의 구성원이면 소유자를 admin 으로
    # 넘기는 순간 app 이 소유자 권한을 잃는다. 그런 원본은 사람이 정리한다.
    app = plan.source_app_user
    if not pg_client.scalar(
        conn, "SELECT 1 FROM pg_roles WHERE rolname = %s", (app,)
    ):
        raise PrincipalError(f"원본 앱 로그인이 없다: {app}")
    owned = []
    for table in plan.tables:
        found = pg_client.find_table(conn, table)
        if found and pg_client.scalar(
            conn, "SELECT pg_has_role(%s, %s, 'MEMBER')", (app, found[1])
        ):
            owned.append(table)
    if owned:
        raise PrincipalError(
            f"원본 앱({app})이 소유한 표: {', '.join(owned)}"
            " — 소유자를 먼저 정리한다"
        )


def _create_database_if_missing(
    conn: psycopg.Connection, database: str
) -> None:
    if pg_client.scalar(
        conn, "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
    ):
        return
    conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))


def _ensure_passwords(
    plan: PrincipalPlan, secret_store: store.SecretStore
) -> tuple[dict[str, bytes], tuple[PreparedLogin, ...]]:
    passwords: dict[str, bytes] = {}
    prepared = []
    for login in plan.logins:
        password = secret_store.read_current(login.secret_id)
        written = None
        if password is None:
            password = secrets.token_urlsafe(_PASSWORD_BYTES).encode()
            written = secret_store.add_version(
                login.secret_id,
                password,
                store.version_token(plan.run_id, login.secret_id),
            )
        passwords[login.purpose] = password
        prepared.append(
            PreparedLogin(login.purpose, login.name, login.secret_id, written)
        )
    return passwords, tuple(prepared)


def _bootstrap_target(
    conn: psycopg.Connection, plan: PrincipalPlan, passwords: dict[str, bytes]
) -> None:
    pg16 = conn.info.server_version >= _PG16
    with conn.transaction():
        for login in plan.logins:
            _ensure_role(conn, login, passwords[login.purpose])
        _grant_database_create(conn, plan, ("admin",))
        if "public" in _schemas(plan.tables):
            conn.execute(
                sql.SQL("GRANT CREATE ON SCHEMA public TO {}").format(
                    _ident(plan, "admin")
                )
            )
        # admin 이 app 의 LOGIN 을 바꾸고(ADMIN) app 세션을 끊을 수 있게
        # (INHERIT) 한다. app 으로 SET ROLE 하지는 못한다.
        options = "WITH ADMIN OPTION, INHERIT TRUE, SET FALSE"
        conn.execute(
            sql.SQL("GRANT {} TO {} {}").format(
                _ident(plan, "app"),
                _ident(plan, "admin"),
                sql.SQL(options if pg16 else "WITH ADMIN OPTION"),
            )
        )


def _bootstrap_source(
    conn: psycopg.Connection, plan: PrincipalPlan, passwords: dict[str, bytes]
) -> None:
    pg16 = conn.info.server_version >= _PG16
    with conn.transaction():
        for login in plan.logins:
            _ensure_role(conn, login, passwords[login.purpose])
        _grant_database_create(conn, plan, ("admin", "cdc"))
        _grant_replication(conn, plan)
        conn.execute(
            sql.SQL("GRANT {} TO {} {}").format(
                _ident(plan, "cdc"),
                _ident(plan, "admin"),
                sql.SQL("WITH INHERIT FALSE, SET TRUE" if pg16 else ""),
            )
        )
        for schema in _schemas(plan.tables):
            # 새 소유자는 표의 스키마에 CREATE 가 있어야 한다(ALTER ... OWNER).
            conn.execute(
                sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema), _ident(plan, "admin")
                )
            )
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}, {}").format(
                    sql.Identifier(schema),
                    _ident(plan, "cdc"),
                    _ident(plan, "verify"),
                )
            )
        _hand_tables_to_admin(conn, plan, pg16)


def _ensure_role(
    conn: psycopg.Connection, login: DbLogin, password: bytes
) -> None:
    verifier = conn.pgconn.encrypt_password(
        password, login.name.encode(), b"scram-sha-256"
    ).decode()
    exists = pg_client.scalar(
        conn, "SELECT 1 FROM pg_roles WHERE rolname = %s", (login.name,)
    )
    # app 은 이미 있어도 NOLOGIN 으로 되돌린다 — PRECHECK 전 대상 쓰기 금지.
    attributes = {"admin": "LOGIN CREATEROLE", "app": "NOLOGIN"}.get(
        login.purpose, "LOGIN"
    )
    conn.execute(
        sql.SQL("{} ROLE {} WITH {} PASSWORD {}").format(
            sql.SQL("ALTER" if exists else "CREATE"),
            sql.Identifier(login.name),
            sql.SQL(attributes),
            sql.Literal(verifier),
        )
    )


def _end_sessions(conn: psycopg.Connection, role: str) -> None:
    """Role 의 세션을 끊고 0 이 될 때까지 기다린다(트랜잭션마다 새로 읽는다)."""
    deadline = time.monotonic() + _SESSION_WAIT_SECONDS
    while True:
        with conn.transaction():
            remaining = conn.execute(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                "WHERE usename = %s AND pid <> pg_backend_pid()",
                (role,),
            ).fetchone()[0]
        if remaining == 0:
            return
        if time.monotonic() > deadline:
            raise PrincipalError(f"{role}: 세션 {remaining}개가 끊기지 않았다")
        time.sleep(_SESSION_POLL_SECONDS)


def _grant_database_create(
    conn: psycopg.Connection,
    plan: PrincipalPlan,
    purposes: Iterable[LoginPurpose],
) -> None:
    conn.execute(
        sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
            sql.Identifier(plan.endpoint.database),
            sql.SQL(", ").join(_ident(plan, p) for p in purposes),
        )
    )


def _grant_replication(conn: psycopg.Connection, plan: PrincipalPlan) -> None:
    cdc = _ident(plan, "cdc")
    if plan.cloud == "aws":
        conn.execute(sql.SQL("GRANT rds_replication TO {}").format(cdc))
    else:
        conn.execute(sql.SQL("ALTER ROLE {} WITH REPLICATION").format(cdc))


def _hand_tables_to_admin(
    conn: psycopg.Connection, plan: PrincipalPlan, pg16: bool
) -> None:
    # 소유자를 바꾸려면 새 소유자로 SET ROLE 할 수 있어야 한다(PG16+ 는 SET
    # 멤버십).
    admin = plan.login("admin").name
    membership = "SET" if pg16 else "MEMBER"
    if not pg_client.scalar(
        conn,
        "SELECT pg_has_role(current_user, %s, %s)",
        (admin, membership),
    ):
        conn.execute(
            sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(admin))
        )
    for table in plan.tables:
        found = pg_client.find_table(conn, table)
        if found is None:
            raise PrincipalError(f"원본에 표가 없다: {table}")
        if found[1] != admin:
            conn.execute(
                sql.SQL("ALTER TABLE {} OWNER TO {}").format(
                    _table_ident(table), sql.Identifier(admin)
                )
            )


def _restore_schema(
    plan: PrincipalPlan,
    master: pg_client.PgLogin,
    admin: pg_client.PgLogin,
    source: pg_client.PgLogin,
    tools: pg_client.PgTools,
) -> None:
    with pg_client.connect(master) as conn:
        present = [t for t in plan.tables if pg_client.find_table(conn, t)]
        prologue = "".join(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {};\n")
            .format(sql.Identifier(s))
            .as_string(conn)
            for s in sorted(_schemas(plan.tables) - {"public"})
        )
    if len(present) == len(plan.tables):
        return
    if present:
        raise PrincipalError(f"대상에 표가 일부만 있다: {', '.join(present)}")
    ddl = tools.dump_schema(source, plan.tables)
    tools.run_script(admin, prologue.encode() + ddl)


def _app_grants(
    source: psycopg.Connection, app_user: str, tables: tuple[str, ...]
) -> tuple["MirroredGrant", ...]:
    """원본 app 의 실효 권한.

    직접 받은 것뿐 아니라 그룹 역할·PUBLIC 에서 상속한 권한도 has_*_privilege
    로 계산한다. 대상에는 이것을 app 에 직접 GRANT 한다.
    """
    pairs = [pg_client.split_table(t) for t in tables]
    rows = source.execute(
        """
        WITH wanted AS (
            SELECT c.oid, n.nspname, c.relname
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN unnest(%(schemas)s::text[], %(names)s::text[]) AS w(s, t)
              ON n.nspname = w.s AND c.relname = w.t
            WHERE c.relkind IN ('r', 'p'))
        SELECT 'TABLE', w.nspname::text, w.relname::text, p.priv
        FROM wanted w CROSS JOIN unnest(%(table_privs)s::text[]) AS p(priv)
        WHERE has_table_privilege(%(app)s, w.oid, p.priv)
        UNION ALL
        SELECT 'SEQUENCE', sn.nspname::text, s.relname::text, p.priv
        FROM wanted w
        JOIN pg_depend d ON d.refobjid = w.oid
         AND d.classid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
        JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S'
        JOIN pg_namespace sn ON sn.oid = s.relnamespace
        CROSS JOIN unnest(%(sequence_privs)s::text[]) AS p(priv)
        WHERE CASE WHEN s.relkind = 'S'
                   THEN has_sequence_privilege(%(app)s, s.oid, p.priv) END
        UNION ALL
        SELECT 'SCHEMA', n.nspname::text, NULL::text, 'USAGE'
        FROM pg_namespace n
        WHERE n.nspname = ANY(%(schemas)s::text[])
          AND has_schema_privilege(%(app)s, n.oid, 'USAGE')
        ORDER BY 1, 2, 3, 4
        """,
        {
            "app": app_user,
            "schemas": [s for s, _ in pairs],
            "names": [t for _, t in pairs],
            "table_privs": list(_TABLE_MIRROR),
            "sequence_privs": list(_SEQUENCE_MIRROR),
        },
    ).fetchall()
    # 같은 스키마의 표가 여럿이면 SCHEMA 행이 겹친다.
    return tuple(MirroredGrant.from_row(row) for row in dict.fromkeys(rows))


def _grant_target(
    conn: psycopg.Connection,
    plan: PrincipalPlan,
    mirrored: tuple["MirroredGrant", ...],
) -> None:
    for schema in _schemas(plan.tables) - {"public"}:
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}, {}").format(
                sql.Identifier(schema),
                _ident(plan, "sink"),
                _ident(plan, "verify"),
            )
        )
    for table in plan.tables:
        conn.execute(
            sql.SQL("GRANT {} ON {} TO {}").format(
                sql.SQL(", ").join(map(sql.SQL, SINK_PRIVILEGES)),
                _table_ident(table),
                _ident(plan, "sink"),
            )
        )
        conn.execute(
            sql.SQL("GRANT SELECT ON {} TO {}").format(
                _table_ident(table), _ident(plan, "verify")
            )
        )
    for grant in mirrored:
        if grant.kind == "SCHEMA" and grant.schema == "public":
            # public USAGE 는 PUBLIC 이 이미 가진다(admin 은 소유자도 아니다).
            continue
        conn.execute(grant.statement(plan.login("app").name))


def _write_profile(
    plan: PrincipalPlan, app_password: bytes, secret_store: store.SecretStore
) -> None:
    # TLS 설정은 URL 이 아니라 앱 env(PGSSLMODE 등)로 준다 — sample-app 은 URL
    # 의 ssl 계열 키를 거부한다. 원본 프로필과 다른 것은 호스트·DB 이름뿐이다.
    if plan.app_profile_secret_id is None:
        return
    endpoint = plan.endpoint
    quote = urllib.parse.quote
    url = (
        f"postgresql://{quote(plan.login('app').name, safe='')}:"
        f"{quote(app_password.decode(), safe='')}@{endpoint.host}:"
        f"{endpoint.port}/{quote(endpoint.database, safe='')}"
    ).encode()
    if secret_store.read_current(plan.app_profile_secret_id) == url:
        return
    secret_store.add_version(
        plan.app_profile_secret_id,
        url,
        store.version_token(plan.run_id, plan.app_profile_secret_id),
    )


def _ident(plan: PrincipalPlan, purpose: LoginPurpose) -> sql.Identifier:
    return sql.Identifier(plan.login(purpose).name)


def _table_ident(table: str) -> sql.Identifier:
    return sql.Identifier(*pg_client.split_table(table))


def _schemas(tables: Iterable[str]) -> set[str]:
    return {pg_client.split_table(t)[0] for t in tables}
