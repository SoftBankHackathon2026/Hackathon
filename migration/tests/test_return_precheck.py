"""귀환 사전 점검 테스트. 판정 로직은 단위 테스트, 실제 DB 수집은 MIGRATION_TEST_DSN 이 있을 때만."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from migration import init_db, return_precheck as rp
from migration.config import load_config, parse_config
from tests.conftest import integration_dsn, requires_pg

ROOT = Path(__file__).resolve().parent.parent

RETURN_RAW = {
    "targets": {
        "aws": {"host": "aws-db", "dbname": "migration_demo", "users": {"admin": {"user": "oneaction_admin"}}},
        "onprem_return": {"host": "postgres", "dbname": "migration_demo_return"},
    },
    "job": {"job_id": "return_001", "source": "aws", "target": "onprem_return",
            "live_phase_before": "aws_live", "live_phase_after": "onprem_return_live"},
}
FORWARD_RAW = {
    "targets": {"onprem": {"host": "postgres", "dbname": "migration_demo"}, "aws": {"host": "aws-db"}},
    "job": {"job_id": "forward_001", "source": "onprem", "target": "aws"},
}
COLS = [{"name": "request_id", "type": "uuid", "not_null": True}, {"name": "phase", "type": "text", "not_null": True}]


def good_aws() -> dict:
    return {
        "settings": {"wal_level": "logical", "rds.logical_replication": "on", "max_replication_slots": "20",
                     "server_version_num": "160015"},
        "extensions": {"plpgsql": "1.0"},
        "table_exists": True,
        "cdc_role": {"exists": True, "rolreplication": False, "rds_replication_member": True},
        "slots": [], "publications": [],
        "columns": COLS, "primary_key": ["request_id"],
        "row_count": 1250, "phase_counts": {"seed": 1000, "onprem_live": 150, "aws_live": 100},
        "privileges": {"app": {"INSERT": True}, "cdc": {"SELECT": True}},
    }


def good_onprem() -> dict:
    return {
        "database_exists": True, "dbname": "migration_demo_return",
        "settings": {"server_version_num": "160004"}, "extensions": {"plpgsql": "1.0"},
        "table_exists": True, "columns": COLS, "primary_key": ["request_id"], "row_count": 0,
        "privileges": {"app": {"INSERT": False}, "sink": {"INSERT": True, "UPDATE": True}, "verify": {"SELECT": True}},
    }


NO_CONNECTORS = {"forward": {"forward_001-source": None, "forward_001-sink": None},
                 "return": {"return_001-source": None, "return_001-sink": None}}


def by_name(checks):
    return {c.name: c for c in checks}


def test_return_example_yaml_loads():
    cfg = load_config(ROOT / "config.return.example.yaml")
    assert (cfg.job.source, cfg.job.target, cfg.job.job_id) == ("aws", "onprem_return", "return_001")
    assert cfg.job.slot_name == "return_001" and cfg.job.publication_name == "return_001_pub"
    assert cfg.target("onprem_return").dbname != load_config(ROOT / "config.example.yaml").target("onprem").dbname
    assert cfg.target("aws").user_spec("admin").user == "oneaction_admin"


def test_all_good_passes():
    cfg, fwd = parse_config(RETURN_RAW), parse_config(FORWARD_RAW)
    checks = rp.evaluate(cfg, good_aws(), good_onprem(), connect=NO_CONNECTORS, forward=fwd,
                         forward_slot={"slot_name": "forward_001", "slot": None})
    assert rp.passed(checks), [c for c in checks if c.ok is False]


@pytest.mark.parametrize("mutate,failed", [
    (lambda a, o: a["settings"].update(wal_level="replica"), "aws.wal_level_logical"),
    (lambda a, o: a["cdc_role"].update(rds_replication_member=False), "aws.cdc_replication"),
    (lambda a, o: a.update(row_count=1000, phase_counts={"seed": 1000}), "aws.not_reset"),
    (lambda a, o: a["phase_counts"].update(seed=999), "aws.seed_preserved"),
    (lambda a, o: o.update(row_count=1000), "onprem.table_empty"),
    (lambda a, o: o["privileges"]["app"].update(INSERT=True), "onprem.app_write_closed"),
    (lambda a, o: o.update(columns=COLS[:1]), "schema.columns_match"),
    (lambda a, o: o.update(primary_key=[]), "schema.primary_key_match"),
])
def test_each_required_failure(mutate, failed):
    aws, onprem = good_aws(), good_onprem()
    mutate(aws, onprem)
    checks = rp.evaluate(parse_config(RETURN_RAW), aws, onprem, connect=NO_CONNECTORS)
    assert by_name(checks)[failed].ok is False
    assert not rp.passed(checks)


def test_forward_connector_left_fails():
    conn = {"forward": {"forward_001-source": "RUNNING", "forward_001-sink": None}, "return": {}}
    checks = rp.evaluate(parse_config(RETURN_RAW), good_aws(), good_onprem(), connect=conn)
    assert by_name(checks)["connect.forward_stopped"].ok is False


def test_target_same_as_forward_source_fails():
    raw = {**RETURN_RAW, "targets": {**RETURN_RAW["targets"],
                                     "onprem_return": {"host": "postgres", "dbname": "migration_demo"}}}
    checks = rp.evaluate(parse_config(raw), good_aws(), good_onprem(), connect=NO_CONNECTORS,
                         forward=parse_config(FORWARD_RAW))
    assert by_name(checks)["onprem.separate_from_forward_source"].ok is False


def test_missing_target_db_points_to_init_db():
    checks = rp.evaluate(parse_config(RETURN_RAW), good_aws(), {"database_exists": False, "dbname": "x"},
                         connect=NO_CONNECTORS)
    c = by_name(checks)["onprem.database_exists"]
    assert c.ok is False and "init_db" in c.detail


def test_warnings_do_not_fail():
    aws = good_aws()
    aws["publications"] = ["forward_001_pub"]
    checks = rp.evaluate(parse_config(RETURN_RAW), aws, good_onprem(), connect=None)
    assert by_name(checks)["aws.no_foreign_cdc_objects"].ok is False
    assert rp.passed(checks)


# ---- 실제 PostgreSQL ---------------------------------------------------------------------

PASSWORDS = {
    "MIGRATION_APP_PASSWORD": "app-pw-1", "MIGRATION_CDC_PASSWORD": "cdc-pw-1",
    "MIGRATION_SINK_PASSWORD": "sink-pw-1", "MIGRATION_VERIFY_PASSWORD": "verify-pw-1",
}


@requires_pg
@pytest.mark.integration
def test_gather_on_real_postgres(monkeypatch):
    """원본(aws 역할) DB 에 seed+운영 데이터를 넣고, 귀환 대상은 init_db 로만 준비해 점검이 통과하는지."""
    dsn = integration_dsn()
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    suffix = uuid.uuid4().hex[:8]
    src_db, tgt_db = f"mig_rsrc_{suffix}", f"mig_rtgt_{suffix}"
    env = {**PASSWORDS, "MIGRATION_ADMIN_PASSWORD": info.get("password", ""),
           "MIGRATION_AWS_ADMIN_PASSWORD": info.get("password", "")}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    base = {"host": info.get("host", "127.0.0.1"), "port": int(info.get("port", 5432)),
            "sslmode": info.get("sslmode", "disable"),
            "users": {"admin": {"user": info.get("user", "postgres"), "password_env": "MIGRATION_ADMIN_PASSWORD"}}}
    cfg = parse_config({
        "targets": {"aws": {**base, "dbname": src_db}, "onprem_return": {**base, "dbname": tgt_db}},
        "job": {"job_id": "return_001", "source": "aws", "target": "onprem_return"},
    })
    sql_path = ROOT / "sql" / "001_demo.sql"
    try:
        assert init_db.run(cfg, "aws", sql_path=sql_path) == 0
        assert init_db.run(cfg, "onprem_return", sql_path=sql_path) == 0
        with psycopg.connect(dsn, dbname=src_db) as conn, conn.cursor() as cur:
            t0 = datetime(2026, 1, 1, tzinfo=UTC)
            rows = [(uuid.uuid4(), "r", "seed" if n <= 1000 else "aws_live", n, "p", t0) for n in range(1, 1051)]
            cur.executemany("INSERT INTO public.migration_demo VALUES (%s,%s,%s,%s,%s,%s)", rows)
        aws, onprem = rp.gather_db(cfg, "aws"), rp.gather_db(cfg, "onprem_return")
        assert aws["row_count"] == 1050 and aws["phase_counts"] == {"seed": 1000, "aws_live": 50}
        assert onprem["row_count"] == 0
        checks = by_name(rp.evaluate(cfg, aws, onprem, connect=NO_CONNECTORS))
        for name in ("aws.seed_preserved", "aws.not_reset", "aws.app_write_open", "aws.cdc_replication",
                     "onprem.table_empty", "onprem.app_write_closed", "onprem.sink_write",
                     "schema.columns_match", "schema.primary_key_match"):
            assert checks[name].ok is True, (name, checks[name].detail)
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for d in (src_db, tgt_db):
                conn.execute(f'DROP DATABASE IF EXISTS "{d}" WITH (FORCE)')
