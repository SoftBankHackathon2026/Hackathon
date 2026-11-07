"""Opt-in disposable PostgreSQL tests. CLOUD_TEST_PORT points to a trust-auth local test container."""

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from migration.cloud.plan import parse
from migration.cloud.postgres import Postgres
from migration.config import ConfigError

pytestmark = pytest.mark.skipif(
    not os.getenv("CLOUD_TEST_PORT"), reason="CLOUD_TEST_PORT not set"
)


@pytest.fixture
def pg(monkeypatch):
    port = int(os.environ["CLOUD_TEST_PORT"])
    suffix = uuid4().hex[:10]
    roles = {k: k + "_" + suffix for k in ("app", "other", "sink", "cdc")}
    names = ["source_" + suffix, "target_" + suffix]

    def conn(name, user="postgres"):
        return psycopg.connect(
            host="127.0.0.1", port=port, dbname=name, user=user, autocommit=True
        )

    with conn("postgres") as admin:
        for role in roles.values():
            admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
        for name in names:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    raw = {
        "targets": {},
        "job": {"job_id": "test_" + suffix, "source": "aws", "target": "gcp"},
        "application": {
            "control_url": "http://localhost:1",
            "tables": [
                {"name": "public.orders", "primary_key": ["tenant", "id"]},
                {"name": "public.empty", "primary_key": ["id"]},
            ],
        },
    }
    for key, name in zip(("aws", "gcp"), names):
        raw["targets"][key] = {
            "host": "127.0.0.1",
            "port": port,
            "dbname": name,
            "sslmode": "disable",
            "users": {
                role: {
                    "user": roles.get(role, "postgres"),
                    "password_env": "CLOUD_TEST_PASSWORD",
                }
                for role in ("admin", "app", "cdc", "sink", "verify")
            },
        }
        with conn(name) as admin:
            admin.execute(
                "CREATE TABLE orders(tenant text, id bigserial, amount numeric(38,9), data jsonb, stamp timestamptz, optional text, PRIMARY KEY(tenant,id))"
            )
            admin.execute("CREATE TABLE empty(id integer PRIMARY KEY, note text)")
            for role in roles.values():
                admin.execute(
                    sql.SQL(
                        "GRANT SELECT,INSERT,UPDATE,DELETE,TRUNCATE ON ALL TABLES IN SCHEMA public TO {}"
                    ).format(sql.Identifier(role))
                )
                admin.execute(
                    sql.SQL(
                        "GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}"
                    ).format(sql.Identifier(role))
                )
    monkeypatch.setenv("CLOUD_TEST_PASSWORD", "unused-local-trust")
    plan = parse(raw)
    database = Postgres(plan)
    yield database, conn, names, roles
    with conn("postgres") as admin:
        for name in names:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
        for role in roles.values():
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def insert(conn):
    conn.execute(
        "INSERT INTO orders(tenant,amount,data,stamp) VALUES ('a',12345678901234567890123456789.123456789,'{\"x\":null}', '2026-11-07T01:02:03.123456Z')"
    )


def test_full_rows_empty_precision_and_missing_extra(pg):
    database, conn, names, roles = pg
    assert database.precheck() == {"tables": 2}
    assert database.verify()["passed"]
    for name in names:
        with conn(name) as c:
            insert(c)
    assert database.verify()["passed"]
    with conn(names[1]) as c:
        c.execute("UPDATE orders SET amount=amount-0.000000001")
    report = database.verify()
    assert not report["passed"] and report["tables"]["public.orders"]["changed"] == 1
    with conn(names[1]) as c:
        c.execute("DELETE FROM orders")
    assert database.verify()["tables"]["public.orders"]["missing"] == 1
    with conn(names[1]) as c:
        c.execute("INSERT INTO orders(tenant) VALUES ('extra')")
    report = database.verify()["tables"]["public.orders"]
    assert report["missing"] == 1 and report["extra"] == 1


def test_all_writers_all_operations_persistent_and_sequences(pg):
    database, conn, names, roles = pg
    with conn(names[0]) as c:
        insert(c)
    database.fence("gcp", initial=True)
    database.assert_fenced("gcp")
    with conn(names[1], roles["sink"]) as c:
        insert(c)
    database.fence("aws")
    for target, name in zip(("aws", "gcp"), names):
        database.assert_fenced(target)
        for role in ("app", "other"):
            with conn(name, roles[role]) as c:
                for statement in (
                    "INSERT INTO empty VALUES (1,'x')",
                    "UPDATE orders SET optional=NULL",
                    "DELETE FROM orders",
                    "TRUNCATE orders",
                    "DELETE FROM empty WHERE false",
                ):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        c.execute(statement)
    database.fence("aws")  # idempotent crash/retry
    assert database.verify()["passed"]
    database.sync_sequences()
    database.release_target()
    with conn(names[1], roles["app"]) as c:
        assert (
            c.execute(
                "INSERT INTO orders(tenant) VALUES ('a') RETURNING id"
            ).fetchone()[0]
            == 2
        )
    database.assert_fenced("aws")


def test_nonempty_target_refused_without_fence(pg):
    database, conn, names, roles = pg
    with conn(names[1]) as c:
        insert(c)
    with pytest.raises(ConfigError):
        database.fence("gcp", initial=True)
    with conn(names[1], roles["app"]) as c:
        c.execute("DELETE FROM orders")


def test_rls_and_primary_key_drift_rejected(pg):
    database, conn, names, roles = pg
    with conn(names[1]) as c:
        c.execute("ALTER TABLE orders ENABLE ROW LEVEL SECURITY")
    with pytest.raises(ConfigError):
        database.precheck()
