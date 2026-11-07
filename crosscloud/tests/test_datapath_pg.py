"""datapath 를 실제 PostgreSQL 에 돌리는 통합 시험(pg_cluster.py 참조)."""

import subprocess

import pg_cluster
import psycopg
import pytest
from memory_secret_store import MemorySecretStore

from crosscloud import datapath, pg_client

pytestmark = pytest.mark.skipif(
    not pg_cluster.DSN, reason="CROSSCLOUD_TEST_PG_DSN 없음"
)

_TABLES = ("public.orders", "shop.items")


@pytest.fixture
def cluster():
    created = pg_cluster.create()
    yield created
    pg_cluster.drop(created)


def _target_plan(cluster, direction="aws_to_gcp"):
    return datapath.PrincipalPlan(
        direction=direction,
        side="target",
        endpoint=cluster.target,
        logins=tuple(
            datapath.DbLogin(
                purpose=purpose,
                name=cluster.name(f"t{purpose}"),
                secret_id=f"tgt-{purpose}",
            )
            for purpose in ("admin", "app", "sink", "verify")
        ),
        tables=_TABLES,
        run_id="ccx-test-forward",
        app_profile_secret_id="tgt-app-profile",
    )


def _source_plan(cluster, direction="aws_to_gcp"):
    return datapath.PrincipalPlan(
        direction=direction,
        side="source",
        endpoint=cluster.source,
        logins=tuple(
            datapath.DbLogin(
                purpose=purpose,
                name=cluster.name(f"s{purpose}"),
                secret_id=f"src-{purpose}",
            )
            for purpose in ("admin", "cdc", "verify")
        ),
        tables=_TABLES,
        run_id="ccx-test-forward",
    )


def _schema_source(cluster):
    return datapath.SchemaSource(
        login=cluster.master_login(cluster.source),
        app_user=cluster.source_app,
    )


def _prepare_target(cluster, store, plan=None):
    plan = plan or _target_plan(cluster)
    with pg_client.connect(cluster.master_login(cluster.target)) as bootstrap:
        return datapath.prepare_db_principals(
            plan,
            bootstrap,
            store,
            schema_source=_schema_source(cluster),
            tools=pg_cluster.TOOLS,
        )


def _prepare_source(cluster, store, plan=None):
    plan = plan or _source_plan(cluster)
    with pg_client.connect(cluster.master_login(cluster.source)) as bootstrap:
        return datapath.prepare_db_principals(plan, bootstrap, store)


def _login_as(cluster, store, endpoint, purpose, side_prefix):
    name = cluster.name(f"{side_prefix}{purpose}")
    secret = ("tgt-" if side_prefix == "t" else "src-") + purpose
    login = pg_client.PgLogin(endpoint, name, store.read_current(secret))
    return pg_client.connect(login)


def _check(cluster, plan, endpoint):
    with pg_client.connect(cluster.master_login(endpoint)) as conn:
        return datapath.check_db_principals(conn, plan)


def _has(conn, role, table, privilege):
    return conn.execute(
        "SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)
    ).fetchone()[0]


def test_target_principals_follow_cutover_login_contract(cluster):
    store = MemorySecretStore()

    prepared = _prepare_target(cluster, store)

    assert {p.purpose for p in prepared} == {"admin", "app", "sink", "verify"}
    assert _check(cluster, _target_plan(cluster), cluster.target) == ()
    for purpose in ("admin", "sink", "verify"):
        with _login_as(cluster, store, cluster.target, purpose, "t") as conn:
            assert conn.execute("SELECT count(*) FROM public.orders").fetchone()
    with pytest.raises(psycopg.OperationalError, match="not permitted"):
        _login_as(cluster, store, cluster.target, "app", "t")


def test_target_schema_is_empty_copy_owned_by_admin(cluster):
    _prepare_target(cluster, MemorySecretStore())

    with cluster.superuser(cluster.target.database) as conn:
        owners = conn.execute(
            "SELECT relname, pg_get_userbyid(relowner) FROM pg_class "
            "WHERE oid IN ('public.orders'::regclass, 'shop.items'::regclass,"
            " 'public.orders_id_seq'::regclass) ORDER BY relname"
        ).fetchall()
        rows = conn.execute("SELECT count(*) FROM public.orders").fetchone()

    admin = cluster.name("tadmin")
    assert owners == [
        ("items", admin),
        ("orders", admin),
        ("orders_id_seq", admin),
    ]
    assert rows == (0,)


def test_target_app_mirrors_source_app_grants(cluster):
    _prepare_target(cluster, MemorySecretStore())

    app = cluster.name("tapp")
    with cluster.superuser(cluster.target.database) as conn:
        assert _has(conn, app, "public.orders", "INSERT")
        assert not _has(conn, app, "public.orders", "UPDATE")
        assert _has(conn, app, "shop.items", "SELECT")
        assert not _has(conn, app, "shop.items", "INSERT")
        usage = conn.execute(
            "SELECT has_sequence_privilege(%s, 'public.orders_id_seq',"
            " 'USAGE')",
            (app,),
        ).fetchone()
    assert usage == (True,)


def test_target_prepare_is_idempotent(cluster):
    store = MemorySecretStore()
    _prepare_target(cluster, store)
    versions = {k: len(v) for k, v in store.versions.items()}

    again = _prepare_target(cluster, store)

    assert {k: len(v) for k, v in store.versions.items()} == versions
    assert all(p.written is None for p in again)
    assert _check(cluster, _target_plan(cluster), cluster.target) == ()


def test_target_profile_secret_points_app_at_target(cluster):
    store = MemorySecretStore()

    _prepare_target(cluster, store)

    profile = store.read_current("tgt-app-profile").decode()
    password = store.read_current("tgt-app").decode()
    host = cluster.target.host
    assert profile == (
        f"postgresql://{cluster.name('tapp')}:{password}@{host}:"
        f"{cluster.target.port}/{cluster.target.database}?sslmode="
        f"{cluster.target.sslmode}"
    )


def test_app_login_opens_and_closes_with_session_termination(cluster):
    store = MemorySecretStore()
    _prepare_target(cluster, store)
    app = cluster.name("tapp")

    with _login_as(cluster, store, cluster.target, "admin", "t") as admin:
        datapath.open_app_login(admin, app)
        app_conn = _login_as(cluster, store, cluster.target, "app", "t")
        app_conn.execute("SELECT 1 FROM shop.items").fetchall()
        app_conn.rollback()

        datapath.close_app_login(admin, app)

    with pytest.raises(psycopg.OperationalError):
        app_conn.execute("SELECT 1").fetchall()
    app_conn.close()
    with pytest.raises(psycopg.OperationalError, match="not permitted"):
        _login_as(cluster, store, cluster.target, "app", "t")


def test_source_principals_can_run_cutover_prepare_steps(cluster):
    store = MemorySecretStore()

    _prepare_source(cluster, store)

    assert _check(cluster, _source_plan(cluster), cluster.source) == ()
    cdc = cluster.name("scdc")
    # 원가연 Postgres.prepare 와 같은 순서: admin 이 publication 을 만들고
    # cdc 에 넘긴다.
    with _login_as(cluster, store, cluster.source, "admin", "s") as admin:
        admin.execute(
            "CREATE PUBLICATION ccx_test_pub"
            " FOR TABLE public.orders, shop.items"
        )
        admin.execute(f'ALTER PUBLICATION ccx_test_pub OWNER TO "{cdc}"')
        # 정리는 소유자가 된 cdc 로 SET ROLE 해서 한다(admin 은 상속 안 함).
        admin.execute(f'SET ROLE "{cdc}"')
        admin.execute("DROP PUBLICATION ccx_test_pub")
    with _login_as(cluster, store, cluster.source, "cdc", "s") as conn:
        assert conn.execute(
            "SELECT count(*) FROM public.orders"
        ).fetchone() == (1,)


def test_source_app_keeps_its_grants_after_owner_change(cluster):
    _prepare_source(cluster, MemorySecretStore())

    with cluster.superuser(cluster.source.database) as conn:
        assert _has(conn, cluster.source_app, "public.orders", "INSERT")
        assert not _has(conn, cluster.source_app, "public.orders", "UPDATE")


def test_gcp_source_cdc_gets_replication_attribute(cluster):
    plan = _source_plan(cluster, direction="gcp_to_aws")
    store = MemorySecretStore()
    # Cloud SQL 의 postgres 는 REPLICATION 을 줄 수 있지만 일반 PostgreSQL
    # 에서는 슈퍼유저만 줄 수 있어 슈퍼유저로 준비한다.
    with cluster.superuser(cluster.source.database) as bootstrap:
        datapath.prepare_db_principals(plan, bootstrap, store)

    assert _check(cluster, plan, cluster.source) == ()


def test_check_reports_missing_principals(cluster):
    problems = _check(cluster, _target_plan(cluster), cluster.target)

    assert any(cluster.name("tadmin") in p for p in problems)
    assert any("public.orders" in p for p in problems)


def test_server_log_never_contains_generated_passwords(cluster):
    if not pg_cluster.CONTAINER:
        pytest.skip("CROSSCLOUD_TEST_PG_CONTAINER 없음")
    store = MemorySecretStore()

    _prepare_target(cluster, store)
    _prepare_source(cluster, store)

    log = subprocess.run(
        ["docker", "logs", pg_cluster.CONTAINER],
        capture_output=True,
        check=True,
        text=True,
    )
    text = log.stdout + log.stderr
    assert "SCRAM-SHA-256$" in text  # log_statement=all 로 역할 문장이 찍혔다.
    for versions in store.versions.values():
        for _, value, _ in versions:
            assert value.decode() not in text
