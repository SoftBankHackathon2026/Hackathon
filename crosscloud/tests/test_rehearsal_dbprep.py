import dataclasses
import json

import memory_secret_store
import pg_cluster
import psycopg
import pytest
import rehearsal_outputs as tf

from crosscloud import datapath, pg_client
from crosscloud.rehearsal import config, dbprep

CFG = config.RehearsalConfig(run=tf.RUN, source_image=tf.IMAGE, seed_rows=5)
ENDPOINT = pg_client.DbEndpoint("db.example", 5432, "shop")


def test_bootstrap_password_reuses_secret_value():
    secrets = memory_secret_store.MemorySecretStore({"pw": b"kept"})
    calls = []

    password = dbprep.ensure_bootstrap_password(
        secrets, "pw", calls.append, "r1"
    )

    assert password == b"kept"
    assert calls == []


def test_bootstrap_password_sets_db_before_writing_secret():
    secrets = memory_secret_store.MemorySecretStore()
    calls = []

    password = dbprep.ensure_bootstrap_password(
        secrets, "pw", calls.append, "r1"
    )

    assert calls == [password]
    assert secrets.read_current("pw") == password
    assert len(password) >= 32


def test_bootstrap_password_leaves_secret_empty_when_db_update_fails():
    secrets = memory_secret_store.MemorySecretStore()

    def fail(password):
        raise dbprep.PrepError("거부")

    with pytest.raises(dbprep.PrepError):
        dbprep.ensure_bootstrap_password(secrets, "pw", fail, "r1")

    assert secrets.read_current("pw") is None


def test_rds_master_reads_managed_secret_json():
    value = json.dumps({"username": "ccxadmin", "password": "p@ss"}).encode()

    login = dbprep.rds_master(value, ENDPOINT)

    assert login.user == "ccxadmin"
    assert login.password == b"p@ss"
    assert "p@ss" not in repr(login)


def test_rds_master_error_does_not_echo_secret():
    with pytest.raises(dbprep.PrepError) as caught:
        dbprep.rds_master(b'{"password": "leak-me"}', ENDPOINT)

    assert "leak-me" not in str(caught.value)


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


class _Session:
    def __init__(self, put, gets):
        self.put_response = put
        self.gets = list(gets)
        self.calls = []

    def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return self.put_response

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.gets.pop(0)


def test_cloud_sql_setter_updates_postgres_and_waits_for_operation(
    monkeypatch,
):
    monkeypatch.setattr(dbprep.time, "sleep", lambda seconds: None)
    session = _Session(
        _Response(200, {"name": "op1"}),
        [
            _Response(200, {"status": "RUNNING"}),
            _Response(200, {"status": "DONE"}),
        ],
    )

    dbprep.cloud_sql_password_setter(session, "proj", "ccx-r1-db")(b"pw")

    method, url, kwargs = session.calls[0]
    assert method == "PUT"
    assert url.endswith("/projects/proj/instances/ccx-r1-db/users")
    assert kwargs["params"] == {"name": "postgres"}
    assert kwargs["json"]["password"] == "pw"
    assert session.calls[-1][1].endswith("/projects/proj/operations/op1")


def test_cloud_sql_setter_reports_http_failure_without_body():
    session = _Session(_Response(403, {"error": "secret-ish"}), [])

    with pytest.raises(dbprep.PrepError, match="HTTP 403") as caught:
        dbprep.cloud_sql_password_setter(session, "p", "i")(b"pw")

    assert "secret-ish" not in str(caught.value)


def test_cloud_sql_setter_fails_on_operation_error():
    session = _Session(
        _Response(200, {"name": "op1"}),
        [_Response(200, {"status": "DONE", "error": {"errors": []}})],
    )

    with pytest.raises(dbprep.PrepError, match="op1"):
        dbprep.cloud_sql_password_setter(session, "p", "i")(b"pw")


def test_seed_refuses_other_tables():
    other = dataclasses.replace(CFG, table="public.orders")

    with pytest.raises(dbprep.PrepError):
        dbprep.seed_source(
            pg_client.PgLogin(ENDPOINT, "m", b"x"),
            other,
            memory_secret_store.MemorySecretStore(),
            "url",
        )


def test_readiness_json_matches_render_handoff_reader():
    items = (dbprep._item("replication_flags", True, "wal_level=logical"),)

    assert json.loads(dbprep.readiness_json(items)) == [
        {
            "name": "replication_flags",
            "ready": True,
            "detail": "wal_level=logical",
        }
    ]


needs_pg = pytest.mark.skipif(
    not pg_cluster.DSN, reason="CROSSCLOUD_TEST_PG_DSN 없음"
)


@pytest.fixture
def seed_db():
    database = f"ccx_seed_{pg_cluster.secrets.token_hex(4)}"
    user = f"shop_app_{pg_cluster.secrets.token_hex(4)}"
    with psycopg.connect(pg_cluster.DSN, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{database}"')
    info = psycopg.conninfo.conninfo_to_dict(pg_cluster.DSN)
    endpoint = pg_client.DbEndpoint(
        str(info.get("host", "127.0.0.1")),
        int(info.get("port", 5432)),
        database,
        str(info.get("sslmode", "disable")),
    )
    master = pg_client.PgLogin(
        endpoint, str(info["user"]), str(info.get("password", "")).encode()
    )
    yield master, user
    with psycopg.connect(pg_cluster.DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
        conn.execute(f'DROP ROLE IF EXISTS "{user}"')


@needs_pg
def test_seed_creates_app_login_rows_and_url_once(seed_db):
    master, user = seed_db
    cfg = dataclasses.replace(CFG, app_user=user)
    secrets = memory_secret_store.MemorySecretStore()

    first = dbprep.seed_source(master, cfg, secrets, "url")
    second = dbprep.seed_source(master, cfg, secrets, "url")

    assert (first, second) == (5, 0)
    url = secrets.read_current("url").decode()
    assert len(secrets.versions["url"]) == 1
    with psycopg.connect(url, sslmode=master.endpoint.sslmode) as app:
        rows = app.execute("SELECT count(*) FROM public.migration_demo")
        assert rows.fetchone()[0] == 5
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            app.execute("DELETE FROM public.migration_demo")


@needs_pg
def test_readiness_reports_missing_logins_and_profile(seed_db):
    master, user = seed_db
    cfg = dataclasses.replace(CFG, app_user=user)
    dbprep.seed_source(
        master, cfg, memory_secret_store.MemorySecretStore(), "u"
    )
    source = datapath.PrincipalPlan(
        direction="aws_to_gcp",
        side="source",
        endpoint=master.endpoint,
        logins=tuple(
            datapath.DbLogin(p, f"mig_{p}_{user}", f"s-{p}")
            for p in ("admin", "cdc", "verify")
        ),
        tables=(dbprep.SEED_TABLE,),
        source_app_user=user,
        run_id="r1",
    )
    target = datapath.PrincipalPlan(
        direction="aws_to_gcp",
        side="target",
        endpoint=master.endpoint,
        logins=tuple(
            datapath.DbLogin(p, f"mig_{p}_{user}", f"s-{p}")
            for p in ("admin", "app", "sink", "verify")
        ),
        tables=(dbprep.SEED_TABLE,),
        source_app_user=user,
        run_id="r1",
        app_profile_secret_id="profile-b",
    )

    items = dbprep.collect_readiness(
        master,
        master,
        (source, target),
        memory_secret_store.MemorySecretStore(),
    )

    by_name = {x.name: x for x in items}
    assert set(by_name) == {
        "replication_flags",
        "migration_logins",
        "target_schema_empty",
        "profile_secrets",
    }
    assert not by_name["migration_logins"].ready
    assert not by_name["target_schema_empty"].ready
    assert not by_name["profile_secrets"].ready
