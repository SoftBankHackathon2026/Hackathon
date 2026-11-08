"""귀환(AWS→온프렘) 방향 cutover: 가짜 포트로 대상·이름·실패 분기 검증. 실제 SQL 판정은 통합 테스트로 확인한다."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import psycopg
import pytest
import yaml
from conftest import integration_dsn, requires_pg
from test_cutover import FakeConnect, FakeDb, FakeVerify, FakeBot, FakeIngress, _build

from migration import init_db
from migration.cdc import load_connector_template_pair, template_variables
from migration.config import Config, parse_config
from migration.cutover import (ABORTED, COMPLETED, COPYING, FENCING, NEEDS_RECOVERY, PRECHECK, READY, ROLLED_BACK,
                               SWITCHING, SYNC_CONFIRMED, VERIFYING, ApprovalRequired, Cutover, CutoverError,
                               PgAdminPort, Ports)
from migration.state import CutoverStore

ROOT = Path(__file__).resolve().parent.parent
SOURCE, TARGET = "aws", "onprem_return"
SOURCE_CONNECTOR, SINK_CONNECTOR = "return_001-source", "return_001-sink"


def _bot():
    return FakeBot()


@pytest.fixture
def return_config() -> Config:
    raw = yaml.safe_load((ROOT / "config.return.example.yaml").read_text(encoding="utf-8"))
    raw["cutover"] = {"poll_interval_seconds": 0, "stage_timeout_seconds": 5, "offset_stable_seconds": 0,
                      "min_live_rows_after_snapshot": 100, "min_rows_after_switch": 100}
    raw["bot"] = {"control_url": "http://bot:9091", "request_endpoint": "http://ingress/api/requests"}
    raw["ingress"] = {"control_url": "http://ingress:9092", "targets": {SOURCE: "aws_db", TARGET: "onprem"}}
    return parse_config(raw)


@pytest.fixture
def fakes() -> dict:
    db = FakeDb()
    db.rows = {SOURCE: 800, TARGET: 0}   # 1000건 예산
    db.seed = {SOURCE: 800, TARGET: 0}
    db.app_insert = {SOURCE: True, TARGET: False}
    return {"bot": _bot(), "ingress": FakeIngress("aws_db"), "connect": FakeConnect(), "db": db, "verify": FakeVerify()}


def _simulate_replication(fakes) -> None:
    """snapshot 과 CDC 를 흉내: 커넥터가 있는 동안 대상이 원본을 따라가고, 워커가 원본에 쓰면 원본이 는다."""
    db, connect = fakes["db"], fakes["connect"]
    orig_count = db.count_rows

    def count_rows(target, phase=None):
        if connect.connectors:
            db.seed[TARGET] = db.seed[SOURCE]
            db.rows[TARGET] = db.rows[SOURCE]
            db.target_rows.add("last-1")
        return orig_count(target, phase)

    db.count_rows = count_rows
    def deliver(count):
        ing = fakes["ingress"]
        target = SOURCE if ing.target == "aws_db" else TARGET
        assert ing.mode == "running" and db.app_insert[target]
        db.rows[target] += count
    fakes["bot"].on_batch = deliver


def _grow_target_commits(fakes):
    pass


def _run_to_failure(config, state_dir, fakes, match: str) -> Cutover:
    _simulate_replication(fakes)
    _grow_target_commits(fakes)
    cut = _build(config, state_dir, fakes, approve="return-approval")
    fakes["_cut"] = cut  # 실패 주입 헬퍼가 현재 단계를 볼 수 있게 둔다
    with pytest.raises(CutoverError, match=match):
        cut.run()
    return cut


# ---- a) PRECHECK ------------------------------------------------------------------------


def test_precheck_gate_follows_cdc_replication_result(return_config, state_dir, fakes):
    """상태 머신 쪽 게이트. rolreplication/rds_replication 판정 자체는 아래 통합 테스트가 확인한다."""
    orig = fakes["db"].precheck
    fakes["db"].precheck = lambda target, *, is_source: {**orig(target, is_source=is_source), "cdc_replication": False}
    cut = _build(return_config, state_dir, fakes)
    with pytest.raises(CutoverError, match="REPLICATION"):
        cut.run()
    assert cut.job.stage == ABORTED and cut.job.checkpoint["failed_stage"] == PRECHECK

    fakes["db"].precheck = orig
    _simulate_replication(fakes)
    retried = _build(return_config, state_dir, fakes)
    with pytest.raises(ApprovalRequired):
        retried.run(retry=True)
    assert retried.job.stage == READY


@requires_pg
@pytest.mark.integration
def test_real_precheck_accepts_rds_replication_member(state_dir, monkeypatch):
    """rolreplication=false 인 CDC 계정이 rds_replication 멤버면 실제 PRECHECK SQL 이 통과하고, 빠지면 막는다."""
    dsn = integration_dsn()
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    suffix = uuid.uuid4().hex[:8]
    src_db, tgt_db = f"mig_rcsrc_{suffix}", f"mig_rctgt_{suffix}"
    for name, value in {"MIGRATION_ADMIN_PASSWORD": info.get("password", ""),
                        "MIGRATION_APP_PASSWORD": "app-pw-1", "MIGRATION_CDC_PASSWORD": "cdc-pw-1",
                        "MIGRATION_SINK_PASSWORD": "sink-pw-1", "MIGRATION_VERIFY_PASSWORD": "verify-pw-1"}.items():
        monkeypatch.setenv(name, value)
    base = {"host": info.get("host", "127.0.0.1"), "port": int(info.get("port", 5432)),
            "sslmode": info.get("sslmode", "disable"),
            "users": {"admin": {"user": info.get("user", "postgres"), "password_env": "MIGRATION_ADMIN_PASSWORD"}}}
    cfg = parse_config({
        "targets": {SOURCE: {**base, "dbname": src_db, "kind": "rds"}, TARGET: {**base, "dbname": tgt_db}},
        "job": {"job_id": "return_001", "source": SOURCE, "target": TARGET,
                "live_phase_before": "aws_live", "live_phase_after": "onprem_return_live"},
    })
    sql_path = ROOT / "sql" / "001_demo.sql"

    with psycopg.connect(dsn, autocommit=True) as conn:
        existed = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'rds_replication'").fetchone() is not None
        had_attr = conn.execute("SELECT rolreplication FROM pg_roles WHERE rolname = 'migration_cdc'").fetchone()
        if not existed:
            conn.execute("CREATE ROLE rds_replication NOLOGIN")
    try:
        assert init_db.run(cfg, SOURCE, sql_path=sql_path) == 0
        assert init_db.run(cfg, TARGET, sql_path=sql_path) == 0
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("ALTER ROLE migration_cdc NOREPLICATION")
            assert conn.execute("SELECT rolreplication, pg_has_role('migration_cdc', 'rds_replication', 'MEMBER') "
                                "FROM pg_roles WHERE rolname = 'migration_cdc'").fetchone() == (False, True)

        # 상태 머신 PRECHECK 는 봇·접수큐·1000건 예산(seed 800)까지 요구하므로 여기서는 SQL 판정만 본다.
        # 게이트 자체는 test_precheck_gate_follows_cdc_replication_result 가 가짜 포트로 확인한다.
        ports = Ports(bot=_bot(), connect=FakeConnect(), db=PgAdminPort(cfg), verify=FakeVerify())
        assert ports.db.precheck(SOURCE, is_source=True)["cdc_replication"] is True

        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("REVOKE rds_replication FROM migration_cdc")
        assert ports.db.precheck(SOURCE, is_source=True)["cdc_replication"] is False
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for name in (src_db, tgt_db):
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            conn.execute("REVOKE rds_replication FROM migration_cdc")
            if had_attr and had_attr[0]:
                conn.execute("ALTER ROLE migration_cdc REPLICATION")
            if not existed:
                conn.execute("DROP ROLE rds_replication")


# ---- b) 이름 ----------------------------------------------------------------------------


def test_connectors_use_return_names(return_config, state_dir, fakes):
    _simulate_replication(fakes)
    _grow_target_commits(fakes)
    cut = _build(return_config, state_dir, fakes, approve="return-approval")
    assert cut.run() == COMPLETED
    connect = fakes["connect"]
    assert connect.puts == [SOURCE_CONNECTOR, SINK_CONNECTOR]
    assert connect.deletes == [SOURCE_CONNECTOR, SINK_CONNECTOR]
    assert not [n for n in connect.puts + connect.deletes if "forward_001" in n]


def test_real_templates_render_return_identifiers(return_config):
    """실제 CLI 가 쓰는 정방향 템플릿에 귀환 설정을 넣어도 정방향 이름이 남지 않는다 (offset 재사용 방지)."""
    docs = load_connector_template_pair("forward", template_variables(return_config))
    assert (docs["source"]["name"], docs["sink"]["name"]) == (SOURCE_CONNECTOR, SINK_CONNECTOR)
    source = docs["source"]["config"]
    assert source["slot.name"] == "return_001"
    assert source["publication.name"] == "return_001_pub"
    assert source["topic.prefix"] == "return_001"
    assert source["database.dbname"] == "migration_demo"
    assert "migration_demo_return" in json.dumps(docs["sink"])
    assert "forward_001" not in json.dumps(docs)


# ---- c) 방향 ----------------------------------------------------------------------------


def test_fence_and_switch_follow_return_direction(return_config, state_dir, fakes):
    _simulate_replication(fakes)
    first = _build(return_config, state_dir, fakes)
    with pytest.raises(ApprovalRequired):
        first.run()
    assert first.job.stage == READY and fakes["db"].calls == ["prepare"]

    _grow_target_commits(fakes)
    cut = _build(return_config, state_dir, fakes, approve="return-approval")
    assert cut.run() == COMPLETED
    bot, db = fakes["bot"], fakes["db"]
    assert db.calls == ["prepare", f"revoke:{SOURCE}", f"grant:{TARGET}", f"drop_slot:{SOURCE}"]
    assert [n for enabled, n in bot.calls if enabled] == [100, 200]
    assert fakes["ingress"].target == "onprem" and bot.generation == "stopped"
    assert db.app_insert == {SOURCE: False, TARGET: True}


# ---- d) SWITCHING 이후 실패: 낡은 AWS 로 자동 원복하지 않는다 ---------------------------------


def _fail_wrong_database(fakes):
    fakes["ingress"].check_app = lambda target: {"ok": target == SOURCE}


def _fail_bot_target(fakes):
    def fail(*args):
        raise RuntimeError("ingress /target 실패")
    fakes["ingress"].set_target = fail


def _fail_final_verify(fakes) -> None:
    """전환 후 건수 확인(VERIFYING)에서 대상 delta 가 한 번 모자란다. 재시도(force-retry)에서는 맞는다."""
    db = fakes["db"]
    orig = db.count_rows_in_scope
    state = {"failed": False}

    def count_rows_in_scope(target, *, since=None, upto=None):
        n = orig(target, since=since, upto=upto)
        if target == TARGET and upto is None and not state["failed"] and fakes.get("_cut") is not None \
                and fakes["_cut"].job.stage == VERIFYING:
            state["failed"] = True
            return n - 101  # 봇 100건을 넣어도 1건 부족
        return n

    db.count_rows_in_scope = count_rows_in_scope


@pytest.mark.parametrize(("arrange", "failed_stage"), [
    (_fail_wrong_database, SWITCHING),
    (_fail_bot_target, SWITCHING),
    (_fail_final_verify, VERIFYING),
])
def test_failure_after_switching_never_reopens_aws(return_config, state_dir, fakes, arrange, failed_stage):
    arrange(fakes)
    cut = _run_to_failure(return_config, state_dir, fakes, NEEDS_RECOVERY)
    db = fakes["db"]
    assert cut.job.stage == NEEDS_RECOVERY and cut.job.checkpoint["failed_stage"] == failed_stage
    assert f"grant:{TARGET}" in db.calls

    with pytest.raises(CutoverError, match="force-retry"):
        _build(return_config, state_dir, fakes, approve="return-approval").run(retry=True)
    with pytest.raises(CutoverError, match="ABORTED 상태에서만"):
        _build(return_config, state_dir, fakes).abort_rollback()

    assert f"grant:{SOURCE}" not in db.calls and db.app_insert[SOURCE] is False
    assert f"drop_slot:{SOURCE}" not in db.calls
    assert _build(return_config, state_dir, fakes).job.stage == NEEDS_RECOVERY


def test_force_retry_after_recovery_completes_without_reopening_aws(return_config, state_dir, fakes):
    _fail_final_verify(fakes)
    _run_to_failure(return_config, state_dir, fakes, NEEDS_RECOVERY)
    retried = _build(return_config, state_dir, fakes, approve="return-approval")
    assert retried.run(force_retry=True) == COMPLETED
    assert f"grant:{SOURCE}" not in fakes["db"].calls and fakes["db"].app_insert[SOURCE] is False


# ---- e) SWITCHING 이전 실패: abort 로 AWS 쓰기를 다시 연다 ------------------------------------


def _fail_fence(fakes) -> None:
    fakes["db"].fence_works = False


def _fail_pre_verify(fakes) -> None:
    """SYNC_CONFIRMED 첫 건수 확인(pre-stop)에서 기준점 이전 건수가 어긋난다."""
    db = fakes["db"]
    orig = db.count_rows_in_scope

    def count_rows_in_scope(target, *, since=None, upto=None):
        n = orig(target, since=since, upto=upto)
        if target == TARGET and upto is not None and fakes.get("_cut") is not None \
                and fakes["_cut"].job.stage == SYNC_CONFIRMED:
            return n - 1
        return n

    db.count_rows_in_scope = count_rows_in_scope


@pytest.mark.parametrize(("arrange", "failed_stage"), [
    (_fail_fence, FENCING),
    (_fail_pre_verify, SYNC_CONFIRMED),
])
def test_failure_before_switching_aborts_and_rollback_reopens_aws(return_config, state_dir, fakes, arrange, failed_stage):
    arrange(fakes)
    cut = _run_to_failure(return_config, state_dir, fakes, ABORTED)
    bot, db, connect = fakes["bot"], fakes["db"], fakes["connect"]
    assert cut.job.stage == ABORTED and cut.job.checkpoint["failed_stage"] == failed_stage
    assert db.app_insert == {SOURCE: False, TARGET: False}
    assert bot.generation == "stopped" and f"grant:{TARGET}" not in db.calls

    out = _build(return_config, state_dir, fakes).abort_rollback()
    assert out["source_app_insert"] is True and out["bot"] == "stopped"
    assert db.app_insert == {SOURCE: True, TARGET: False}
    assert fakes["ingress"].target == "aws_db" and bot.generation == "stopped"
    assert connect.connectors == {} and set(connect.deletes) >= {SOURCE_CONNECTOR, SINK_CONNECTOR}
    assert _build(return_config, state_dir, fakes).job.stage == ROLLED_BACK


def test_abort_refused_when_target_writable(return_config, state_dir, fakes):
    """대상 쓰기가 열려 있으면 AWS 를 다시 열지 않는다. 동시 writer 를 만들지 않기 위해서다."""
    _fail_fence(fakes)
    _run_to_failure(return_config, state_dir, fakes, ABORTED)
    fakes["db"].app_insert[TARGET] = True
    with pytest.raises(CutoverError, match="자동 원복 불가"):
        _build(return_config, state_dir, fakes).abort_rollback()
    assert fakes["db"].app_insert[SOURCE] is False and f"grant:{SOURCE}" not in fakes["db"].calls
