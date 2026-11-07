"""datapath 준비 결과를 카탈로그 질의로 확인한다(원가연 PRECHECK 전 점검).

  with pg_client.connect(master) as conn:
      problems = db_readiness.check_db_principals(conn, plan)

빈 tuple 이면 준비된 것이다. 문제는 사람이 읽는 한 줄씩이고 비밀값은 없다.
대상 app 은 쓰기 개방 전 상태(NOLOGIN·세션 0)여야 준비된 것으로 본다.
"""

import psycopg

from crosscloud import datapath, pg_client

_scalar = pg_client.scalar


def check_db_principals(
    conn: psycopg.Connection, plan: datapath.PrincipalPlan
) -> tuple[str, ...]:
    """Plan 대로 준비됐는지 확인한다. conn 은 그 DB 의 관리 사용자 연결."""
    problems = [p for x in plan.logins for p in _role_problems(conn, x)]
    roles_exist = all(
        _scalar(conn, "SELECT 1 FROM pg_roles WHERE rolname = %s", (x.name,))
        for x in plan.logins
    )
    for table in plan.tables:
        problems.extend(_table_problems(conn, plan, table, roles_exist))
    if not roles_exist:
        return tuple(problems)  # 없는 역할의 권한은 물을 수 없다.
    admin = plan.login("admin").name
    if not _scalar(
        conn,
        "SELECT has_database_privilege(%s, current_database(), 'CREATE')",
        (admin,),
    ):
        problems.append(f"{admin}: DB CREATE 권한 없음")
    if plan.side == "source":
        problems.extend(_cdc_problems(conn, plan))
    else:
        problems.extend(_admin_over_app_problems(conn, plan))
        problems.extend(_app_problems(conn, plan))
    return tuple(problems)


def _role_problems(
    conn: psycopg.Connection, login: datapath.DbLogin
) -> list[str]:
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
    plan: datapath.PrincipalPlan,
    table: str,
    roles_exist: bool,
) -> list[str]:
    found = pg_client.find_table(conn, table)
    if found is None:
        return [f"{table}: 표 없음"]
    oid, owner = found
    problems = []
    if owner != plan.login("admin").name:
        problems.append(f"{table}: 소유자 {owner} — admin 이어야 한다")
    if not roles_exist:
        return problems
    needs: dict[datapath.LoginPurpose, tuple[str, ...]] = {
        "verify": ("SELECT",)
    }
    if plan.side == "target":
        needs["sink"] = datapath.SINK_PRIVILEGES
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


def _cdc_problems(
    conn: psycopg.Connection, plan: datapath.PrincipalPlan
) -> list[str]:
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


def _admin_over_app_problems(
    conn: psycopg.Connection, plan: datapath.PrincipalPlan
) -> list[str]:
    """대상 admin 이 app 의 LOGIN 을 바꿀 수 있는지(CREATEROLE + ADMIN)."""
    admin, app = plan.login("admin").name, plan.login("app").name
    problems = []
    if not _scalar(
        conn, "SELECT rolcreaterole FROM pg_roles WHERE rolname = %s", (admin,)
    ):
        problems.append(f"{admin}: CREATEROLE 없음")
    if not _scalar(
        conn,
        "SELECT coalesce(bool_or(m.admin_option), false) "
        "FROM pg_auth_members m "
        "WHERE m.roleid = %s::regrole AND m.member = %s::regrole",
        (app, admin),
    ):
        problems.append(f"{admin}: {app} 의 ADMIN 옵션 없음")
    return problems


def _app_problems(
    conn: psycopg.Connection, plan: datapath.PrincipalPlan
) -> list[str]:
    """대상 app: 세션 0, 스키마 USAGE, 표 SELECT, 쓰는 표의 시퀀스 USAGE.

    읽기만 하는 표가 있을 수 있어, 쓰기(DML)는 지정 표 중 하나 이상이면 된다.
    """
    app = plan.login("app").name
    problems = []
    sessions = _scalar(
        conn, "SELECT count(*) FROM pg_stat_activity WHERE usename = %s", (app,)
    )
    if sessions:
        problems.append(f"{app}: 열린 세션 {sessions}개 — 쓰기 개방 전에는 0")
    writes_anywhere = False
    for table in plan.tables:
        schema, _ = pg_client.split_table(table)
        if not _scalar(
            conn, "SELECT has_schema_privilege(%s, %s, 'USAGE')", (app, schema)
        ):
            problems.append(f"{schema}: {app} 에 USAGE 없음")
        found = pg_client.find_table(conn, table)
        if found is None:
            continue
        table_problems, writes = _app_table_problems(conn, app, table, found[0])
        problems.extend(table_problems)
        writes_anywhere = writes_anywhere or writes
    if not writes_anywhere:
        problems.append(f"{app}: 지정 표 어디에도 쓰기(DML) 권한 없음")
    return problems


def _app_table_problems(
    conn: psycopg.Connection, app: str, table: str, oid: int
) -> tuple[list[str], bool]:
    """표 하나의 문제와, app 이 그 표에 쓸 수 있는지."""

    def has(privilege: str) -> bool:
        return bool(
            _scalar(
                conn,
                "SELECT has_table_privilege(%s, %s, %s)",
                (app, oid, privilege),
            )
        )

    problems = []
    if not has("SELECT"):
        problems.append(f"{table}: {app} 에 SELECT 없음")
    writes = [p for p in ("INSERT", "UPDATE", "DELETE") if has(p)]
    if "INSERT" not in writes:
        return problems, bool(writes)
    missing = conn.execute(
        "SELECT s.relname FROM pg_depend d "
        "JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S' "
        "WHERE d.refobjid = %s AND d.classid = 'pg_class'::regclass "
        "AND d.deptype IN ('a', 'i') "
        # 플래너가 relkind 거르기 전에 함수를 부를 수 있어 CASE 로 감싼다.
        "AND CASE WHEN s.relkind = 'S' "
        "THEN NOT has_sequence_privilege(%s, s.oid, 'USAGE') END",
        (oid, app),
    ).fetchall()
    problems.extend(
        f"{table}: 시퀀스 {s} 에 {app} USAGE 없음" for (s,) in missing
    )
    return problems, True
