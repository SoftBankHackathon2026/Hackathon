"""원가연 전환 도구(migration.cloud)가 쓰는 DB 로그인과 대상 스키마를 준비한다.

원가연 config 의 users 역할(admin·app·cdc·sink·verify)에 맞춰 한쪽 DB 에
로그인을 만든다. 원본은 admin·cdc·verify, 대상은 admin·app·sink·verify 다.

*   admin: 지정 표의 소유자. 원가연이 publication·쓰기 차단 트리거·잠금을
    admin 으로 실행한다. DB CREATE 권한. 대상 admin 은 app 의 LOGIN 을 켜고
    끄도록 CREATEROLE 과 app 의 ADMIN 옵션을 가진다.
*   app(대상): 원본 앱 로그인의 표·시퀀스 권한을 그대로 미러한다. 표를 소유하지
    않고 NOLOGIN 으로 만든다 — 쓰기 개방(/target) 때 open_app_login 이 켠다.
    그 전까지 대상에 쓸 수 있는 앱 로그인이 없다(단일 쓰기 DB).
*   cdc(원본): 복제 권한(RDS 는 rds_replication, Cloud SQL 은 REPLICATION)과
    SELECT. admin 이 publication 소유자를 cdc 로 넘길 수 있게 SET 멤버십.
*   sink(대상): 지정 표 DML. verify: 지정 표 SELECT.

비밀번호는 로그인마다 Secret 하나가 정본이다. 값이 있으면 그 값을, 없으면 새로
만들어 Secret 에 먼저 쓴 뒤 DB 에 넣는다 — 그래서 다시 돌려도 같은 비밀번호다.
DB 에는 클라이언트가 계산한 SCRAM 검증자만 보내 서버 로그에 평문이 남지 않는다.

대상 스키마는 원본에서 `pg_dump --schema-only --no-owner --no-privileges` 로
떠서 대상 admin 으로 복원한다. 데이터는 넣지 않는다(원가연 PRECHECK 가 빈
대상을 요구한다). 빠진 권한은 이 모듈이 GRANT 로 채운다.

방향(aws_to_gcp·gcp_to_aws)과 쪽(source·target)으로 그 DB 의 클라우드가
정해지고, 클라우드마다 복제 권한을 주는 방법만 다르다.
"""

import dataclasses
import secrets
import urllib.parse
from collections.abc import Iterable
from typing import Literal

import psycopg
from psycopg import sql

from crosscloud import model, pg_client
from crosscloud.secret_transfer import store

LoginPurpose = Literal["admin", "app", "cdc", "sink", "verify"]
Side = Literal["source", "target"]

_REQUIRED: dict[Side, frozenset[str]] = {
    "source": frozenset({"admin", "cdc", "verify"}),
    "target": frozenset({"admin", "app", "sink", "verify"}),
}
_TABLE_MIRROR = ("SELECT", "INSERT", "UPDATE", "DELETE")
_SEQUENCE_MIRROR = ("USAGE", "SELECT", "UPDATE")
_SINK_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
_PASSWORD_BYTES = 32
_PG16 = 160000
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
        endpoint: 이 DB 의 주소.
        logins: 쪽마다 정해진 역할 전부(원본 admin·cdc·verify, 대상
            admin·app·sink·verify).
        tables: 옮길 표(원가연 application.tables 의 소문자 schema.table).
        run_id: 이 실행의 식별자. Secret 쓰기 토큰의 재료다.
        app_profile_secret_id: 대상만. 대상 앱이 쓸 접속 URL 을 담을 Secret.
    """

    direction: model.Direction
    side: Side
    endpoint: pg_client.DbEndpoint
    logins: tuple[DbLogin, ...]
    tables: tuple[str, ...]
    run_id: str
    app_profile_secret_id: str | None = None

    def __post_init__(self) -> None:
        """역할 구성과 표 이름을 검사한다.

        Raises:
            ValueError: 쪽에 맞는 역할이 정확히 하나씩이 아니거나, 이름이
                겹치거나, 표 이름 형식이 틀리거나, 원본에 프로필 Secret 이
                있을 때.
        """
        purposes = [login.purpose for login in self.logins]
        if sorted(purposes) != sorted(_REQUIRED[self.side]):
            required = sorted(_REQUIRED[self.side])
            raise ValueError(f"{self.side} 로그인은 {required} 하나씩이다")
        if len({login.name for login in self.logins}) != len(self.logins):
            raise ValueError("로그인 이름이 겹친다")
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
class SchemaSource:
    """대상 준비에 필요한 원본 쪽 정보.

    Attributes:
        login: 원본 DB 로그인(스키마 덤프·권한 읽기. 표 SELECT 가 필요하다).
        app_user: 원본 앱 로그인. 대상 app 이 이 로그인의 권한을 미러한다.
    """

    login: pg_client.PgLogin
    app_user: str


@dataclasses.dataclass(frozen=True)
class PreparedLogin:
    """준비한 로그인 하나. written 은 이번 실행이 새로 쓴 비밀번호 버전."""

    purpose: LoginPurpose
    name: str
    secret_id: str
    written: store.WrittenVersion | None


class PrincipalError(Exception):
    """준비를 이어 갈 수 없는 DB 상태(표가 일부만 있음 등)."""


def prepare_db_principals(
    plan: PrincipalPlan,
    bootstrap: psycopg.Connection,
    secret_store: store.SecretStore,
    schema_source: SchemaSource | None = None,
    tools: pg_client.PgTools = _PATH_TOOLS,
) -> tuple[PreparedLogin, ...]:
    """Plan 의 로그인을 만들고 권한을 맞춘다. 다시 돌려도 같은 결과다.

    Args:
        plan: 만들 로그인과 표.
        bootstrap: 그 DB 관리 사용자(RDS master·Cloud SQL postgres) 연결.
            CREATEROLE 이 있어야 하고 열린 트랜잭션이 없어야 한다.
        secret_store: 이 DB 와 같은 클라우드의 Secret 저장소.
        schema_source: 대상일 때만, 필수. 스키마와 앱 권한을 읽을 원본.
        tools: 대상 스키마 복원에 쓸 pg_dump·psql.

    Returns:
        로그인마다 준비 결과.

    Raises:
        ValueError: 대상인데 schema_source 가 없거나, bootstrap 에 열린
            트랜잭션이 있을 때.
        PrincipalError: 대상 표가 일부만 있을 때.
        pg_client.PgToolError: 스키마 덤프·복원 실패.
        store.SecretStoreError: 비밀번호 Secret 읽기·쓰기 실패.
    """
    if plan.side == "target" and schema_source is None:
        raise ValueError("대상 준비에는 schema_source 가 필요하다")
    if bootstrap.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise ValueError("bootstrap 연결에 열린 트랜잭션이 있다")
    passwords, prepared = _ensure_passwords(plan, secret_store)
    admin = pg_client.PgLogin(
        plan.endpoint, plan.login("admin").name, passwords["admin"]
    )
    if plan.side == "target":
        _prepare_target(plan, bootstrap, passwords, admin, schema_source, tools)
        _write_profile(plan, passwords["app"], secret_store)
    else:
        _prepare_source(plan, bootstrap, passwords, admin)
    return prepared


def open_app_login(admin: psycopg.Connection, app: str) -> None:
    """대상 app 로그인을 켠다(쓰기 개방 때). admin 은 대상 admin 연결."""
    with admin.transaction():
        admin.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN").format(sql.Identifier(app))
        )


def close_app_login(admin: psycopg.Connection, app: str) -> None:
    """대상 app 로그인을 끄고, 이미 붙은 app 세션을 끊는다."""
    with admin.transaction():
        admin.execute(
            sql.SQL("ALTER ROLE {} WITH NOLOGIN").format(sql.Identifier(app))
        )
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE usename = %s AND pid <> pg_backend_pid()",
            (app,),
        )


def check_db_principals(
    conn: psycopg.Connection, plan: PrincipalPlan
) -> tuple[str, ...]:
    """Plan 대로 준비됐는지 카탈로그 질의로 확인한다. 빈 tuple 이면 준비됨.

    대상 app 은 아직 NOLOGIN 이어야 준비된 것으로 본다(쓰기 개방 전 점검용).
    """
    problems = [p for x in plan.logins for p in _role_problems(conn, x)]
    roles_exist = all(
        _scalar(conn, "SELECT 1 FROM pg_roles WHERE rolname = %s", (x.name,))
        for x in plan.logins
    )
    for table in plan.tables:
        problems.extend(_table_problems(conn, plan, table, roles_exist))
    if not roles_exist:
        return tuple(problems)
    admin = plan.login("admin").name
    if not _scalar(
        conn,
        "SELECT has_database_privilege(%s, current_database(), 'CREATE')",
        (admin,),
    ):
        problems.append(f"{admin}: DB CREATE 권한 없음")
    if plan.side == "source":
        problems.extend(_cdc_problems(conn, plan))
    return tuple(problems)


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


def _prepare_target(
    plan: PrincipalPlan,
    bootstrap: psycopg.Connection,
    passwords: dict[str, bytes],
    admin: pg_client.PgLogin,
    schema_source: SchemaSource,
    tools: pg_client.PgTools,
) -> None:
    pg16 = bootstrap.info.server_version >= _PG16
    with bootstrap.transaction():
        for login in plan.logins:
            _ensure_role(bootstrap, login, passwords[login.purpose])
        _grant_database_create(bootstrap, plan, ("admin",))
        if any(t.startswith("public.") for t in plan.tables):
            bootstrap.execute(
                sql.SQL("GRANT CREATE ON SCHEMA public TO {}").format(
                    _ident(plan, "admin")
                )
            )
        options = "WITH ADMIN OPTION, INHERIT TRUE, SET FALSE"
        bootstrap.execute(
            sql.SQL("GRANT {} TO {} {}").format(
                _ident(plan, "app"),
                _ident(plan, "admin"),
                sql.SQL(options if pg16 else "WITH ADMIN OPTION"),
            )
        )
    _restore_schema(plan, bootstrap, admin, schema_source.login, tools)
    with pg_client.connect(schema_source.login) as source:
        mirrored = _app_grants(source, schema_source.app_user, plan.tables)
    with pg_client.connect(admin) as conn, conn.transaction():
        _grant_target(conn, plan, mirrored)


def _prepare_source(
    plan: PrincipalPlan,
    bootstrap: psycopg.Connection,
    passwords: dict[str, bytes],
    admin: pg_client.PgLogin,
) -> None:
    pg16 = bootstrap.info.server_version >= _PG16
    with bootstrap.transaction():
        for login in plan.logins:
            _ensure_role(bootstrap, login, passwords[login.purpose])
        _grant_database_create(bootstrap, plan, ("admin", "cdc"))
        _grant_replication(bootstrap, plan)
        bootstrap.execute(
            sql.SQL("GRANT {} TO {} {}").format(
                _ident(plan, "cdc"),
                _ident(plan, "admin"),
                sql.SQL("WITH INHERIT FALSE, SET TRUE" if pg16 else ""),
            )
        )
        for schema in _schemas(plan.tables):
            # 새 소유자는 표의 스키마에 CREATE 가 있어야 한다(ALTER ... OWNER).
            bootstrap.execute(
                sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema), _ident(plan, "admin")
                )
            )
            bootstrap.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}, {}").format(
                    sql.Identifier(schema),
                    _ident(plan, "cdc"),
                    _ident(plan, "verify"),
                )
            )
        _hand_tables_to_admin(bootstrap, plan, pg16)
    with pg_client.connect(admin) as conn, conn.transaction():
        for table in plan.tables:
            conn.execute(
                sql.SQL("GRANT SELECT ON {} TO {}, {}").format(
                    _table_ident(table),
                    _ident(plan, "cdc"),
                    _ident(plan, "verify"),
                )
            )


def _ensure_role(
    conn: psycopg.Connection, login: DbLogin, password: bytes
) -> None:
    verifier = conn.pgconn.encrypt_password(
        password, login.name.encode(), b"scram-sha-256"
    ).decode()
    exists = _scalar(
        conn, "SELECT 1 FROM pg_roles WHERE rolname = %s", (login.name,)
    )
    # app 은 처음 만들 때만 NOLOGIN 이다. 이미 있으면 LOGIN 상태를 건드리지
    # 않는다 — 쓰기 개방 뒤 다시 돌려도 서비스를 끊지 않게.
    attributes = {
        "admin": "LOGIN CREATEROLE",
        "app": "" if exists else "NOLOGIN",
    }.get(login.purpose, "LOGIN")
    verb = "ALTER" if exists else "CREATE"
    conn.execute(
        sql.SQL("{} ROLE {} WITH {} PASSWORD {}").format(
            sql.SQL(verb),
            sql.Identifier(login.name),
            sql.SQL(attributes),
            sql.Literal(verifier),
        )
    )


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
    if not _scalar(
        conn,
        "SELECT pg_has_role(current_user, %s, %s)",
        (admin, membership),
    ):
        conn.execute(
            sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(admin))
        )
    for table in plan.tables:
        found = _table_row(conn, table)
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
    bootstrap: psycopg.Connection,
    admin: pg_client.PgLogin,
    source: pg_client.PgLogin,
    tools: pg_client.PgTools,
) -> None:
    with bootstrap.transaction():
        present = [t for t in plan.tables if _table_row(bootstrap, t)]
    if len(present) == len(plan.tables):
        return
    if present:
        raise PrincipalError(f"대상에 표가 일부만 있다: {', '.join(present)}")
    schemas = sorted(_schemas(plan.tables) - {"public"})
    prologue = "".join(
        sql.SQL("CREATE SCHEMA IF NOT EXISTS {};\n")
        .format(sql.Identifier(s))
        .as_string(bootstrap)
        for s in schemas
    )
    ddl = tools.dump_schema(source, plan.tables)
    tools.run_script(admin, prologue.encode() + ddl)


def _app_grants(
    source: psycopg.Connection, app_user: str, tables: tuple[str, ...]
) -> tuple[tuple[str, str, str], ...]:
    """원본 app 이 지정 표와 그 소유 시퀀스에 가진 권한(종류, 객체, 권한)."""
    rows = source.execute(
        """
        SELECT CASE c.relkind WHEN 'S' THEN 'SEQUENCE' ELSE 'TABLE' END,
               quote_ident(n.nspname) || '.' || quote_ident(c.relname),
               a.privilege_type
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(c.relacl) a
        WHERE a.grantee = (SELECT oid FROM pg_roles WHERE rolname = %(app)s)
          AND (c.oid = ANY(%(tables)s::text[]::regclass[])
               OR (c.relkind = 'S' AND EXISTS (
                   SELECT 1 FROM pg_depend d
                   WHERE d.classid = 'pg_class'::regclass
                     AND d.objid = c.oid
                     AND d.deptype IN ('a', 'i')
                     AND d.refobjid = ANY(%(tables)s::text[]::regclass[]))))
        ORDER BY 1, 2, 3
        """,
        {"app": app_user, "tables": list(tables)},
    ).fetchall()
    allowed = {"TABLE": _TABLE_MIRROR, "SEQUENCE": _SEQUENCE_MIRROR}
    return tuple(row for row in rows if row[2] in allowed[row[0]])


def _grant_target(
    conn: psycopg.Connection,
    plan: PrincipalPlan,
    mirrored: tuple[tuple[str, str, str], ...],
) -> None:
    grantees = sql.SQL(", ").join(
        _ident(plan, p) for p in ("app", "sink", "verify")
    )
    for schema in _schemas(plan.tables) - {"public"}:
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), grantees
            )
        )
    for table in plan.tables:
        conn.execute(
            sql.SQL("GRANT {} ON {} TO {}").format(
                sql.SQL(", ").join(map(sql.SQL, _SINK_PRIVILEGES)),
                _table_ident(table),
                _ident(plan, "sink"),
            )
        )
        conn.execute(
            sql.SQL("GRANT SELECT ON {} TO {}").format(
                _table_ident(table), _ident(plan, "verify")
            )
        )
    for kind, quoted_name, privilege in mirrored:
        # quoted_name 은 원본 서버가 quote_ident 로 만든 식별자다.
        conn.execute(
            sql.SQL("GRANT {} ON {} {} TO {}").format(
                sql.SQL(privilege),
                sql.SQL(kind),
                sql.SQL(quoted_name),
                _ident(plan, "app"),
            )
        )


def _write_profile(
    plan: PrincipalPlan, app_password: bytes, secret_store: store.SecretStore
) -> None:
    if plan.app_profile_secret_id is None:
        return
    endpoint = plan.endpoint
    quote = urllib.parse.quote
    url = (
        f"postgresql://{quote(plan.login('app').name, safe='')}:"
        f"{quote(app_password.decode(), safe='')}@{endpoint.host}:"
        f"{endpoint.port}/{quote(endpoint.database, safe='')}"
        f"?sslmode={endpoint.sslmode}"
    ).encode()
    if secret_store.read_current(plan.app_profile_secret_id) == url:
        return
    secret_store.add_version(
        plan.app_profile_secret_id,
        url,
        store.version_token(plan.run_id, plan.app_profile_secret_id),
    )


def _role_problems(conn: psycopg.Connection, login: DbLogin) -> list[str]:
    row = conn.execute(
        "SELECT rolcanlogin, rolsuper OR rolbypassrls FROM pg_roles "
        "WHERE rolname = %s",
        (login.name,),
    ).fetchone()
    if row is None:
        return [f"{login.name}: 로그인 없음"]
    can_login, privileged = row
    expect_login = login.purpose != "app"
    problems = []
    if can_login != expect_login:
        state = "LOGIN" if expect_login else "NOLOGIN"
        problems.append(f"{login.name}: {state} 이어야 한다")
    if privileged:
        problems.append(f"{login.name}: 슈퍼유저·RLS 우회 금지")
    return problems


def _table_problems(
    conn: psycopg.Connection,
    plan: PrincipalPlan,
    table: str,
    roles_exist: bool,
) -> list[str]:
    found = _table_row(conn, table)
    if found is None:
        return [f"{table}: 표 없음"]
    oid, owner = found
    problems = []
    if owner != plan.login("admin").name:
        problems.append(f"{table}: 소유자 {owner} — admin 이어야 한다")
    if not roles_exist:
        return problems  # 없는 역할의 권한은 물을 수 없다.
    needs = {"verify": ("SELECT",)}
    if plan.side == "target":
        needs["sink"] = _SINK_PRIVILEGES
        if _scalar(
            conn,
            "SELECT pg_has_role(%s, %s, 'MEMBER')",
            (plan.login("app").name, owner),
        ):
            problems.append(f"{table}: app 이 소유 역할 구성원이다")
    else:
        needs["cdc"] = ("SELECT",)
    for purpose, privileges in needs.items():
        name = plan.login(purpose).name
        for privilege in privileges:
            if not _scalar(
                conn,
                "SELECT has_table_privilege(%s, %s, %s)",
                (name, oid, privilege),
            ):
                problems.append(f"{table}: {name} 에 {privilege} 없음")
    return problems


def _cdc_problems(conn: psycopg.Connection, plan: PrincipalPlan) -> list[str]:
    cdc = plan.login("cdc").name
    admin = plan.login("admin").name
    if plan.cloud == "aws":
        replicates = _scalar(
            conn,
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
            "'rds_replication') AND pg_has_role(%s, 'rds_replication', "
            "'MEMBER')",
            (cdc,),
        )
    else:
        replicates = _scalar(
            conn,
            "SELECT rolreplication FROM pg_roles WHERE rolname = %s",
            (cdc,),
        )
    problems = [] if replicates else [f"{cdc}: 복제 권한 없음"]
    if not _scalar(conn, "SELECT pg_has_role(%s, %s, 'MEMBER')", (admin, cdc)):
        problems.append(
            f"{admin}: {cdc} 의 구성원이 아니다(publication 이관 불가)"
        )
    return problems


def _table_row(conn: psycopg.Connection, table: str) -> tuple[int, str] | None:
    """표의 (oid, 소유자). 스키마 USAGE 없이도 읽히게 이름으로 찾는다."""
    schema, name = pg_client.split_table(table)
    return conn.execute(
        "SELECT c.oid, pg_get_userbyid(c.relowner) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')",
        (schema, name),
    ).fetchone()


def _scalar(conn: psycopg.Connection, query: str, params: tuple) -> object:
    row = conn.execute(query, params).fetchone()
    return None if row is None else row[0]


def _ident(plan: PrincipalPlan, purpose: LoginPurpose) -> sql.Identifier:
    return sql.Identifier(plan.login(purpose).name)


def _table_ident(table: str) -> sql.Identifier:
    return sql.Identifier(*pg_client.split_table(table))


def _schemas(tables: Iterable[str]) -> set[str]:
    return {pg_client.split_table(t)[0] for t in tables}
