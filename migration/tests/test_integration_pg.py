"""실제 PostgreSQL 통합 테스트. MIGRATION_TEST_DSN (예: postgresql://postgres:pw@127.0.0.1:55432/postgres) 가 있을 때만."""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from migration import db as dbmod
from migration import init_db, seed, verify
from migration.cdc import drop_slot, prepare_source
from migration.config import parse_config
from migration.cutover import PgAdminPort
from migration.loadgen import Outcome, PgBackend
from migration.state import RequestRecord, RequestStore
from tests.conftest import integration_dsn, requires_pg

pytestmark = [requires_pg, pytest.mark.integration]

PASSWORDS = {
    "MIGRATION_APP_PASSWORD": "app-pw-1",
    "MIGRATION_CDC_PASSWORD": "cdc-pw-1",
    "MIGRATION_SINK_PASSWORD": "sink-pw-1",
    "MIGRATION_VERIFY_PASSWORD": "verify-pw-1",
}


@pytest.fixture(scope="module")
def pg():
    """DSN 에서 호스트 정보를 뽑아 onprem/aws 두 target 이 같은 인스턴스의 다른 DB 를 가리키게 한다."""
    dsn = integration_dsn()
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    admin_pw = str(info.get("password") or "")
    suffix = uuid.uuid4().hex[:8]
    names = {"onprem": f"mig_src_{suffix}", "aws": f"mig_tgt_{suffix}"}
    raw = {
        "targets": {
            name: {"host": info.get("host", "127.0.0.1"), "port": int(info.get("port", 5432)), "dbname": dbname,
                   "sslmode": str(info.get("sslmode", "disable")), "admin_dbname": str(info.get("dbname", "postgres")),
                   "users": {"admin": {"user": str(info.get("user", "postgres")), "password_env": "MIGRATION_ADMIN_PASSWORD"}}}
            for name, dbname in names.items()
        },
        "job": {"job_id": f"it_{suffix}", "source": "onprem", "target": "aws"},
    }
    env = {"MIGRATION_ADMIN_PASSWORD": admin_pw, **PASSWORDS}
    config = parse_config(raw)
    yield config, env
    with psycopg.connect(dsn, autocommit=True) as conn:
        for dbname in names.values():
            conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


@pytest.fixture
def patched_env(pg, monkeypatch):
    for k, v in pg[1].items():
        monkeypatch.setenv(k, v)
    return pg[0]


def test_init_db_is_idempotent_and_roles_are_minimal(patched_env):
    config = patched_env
    sql_path = Path(__file__).resolve().parent.parent / "sql" / "001_demo.sql"
    assert init_db.run(config, "onprem", sql_path=sql_path) == 0
    assert init_db.run(config, "onprem", sql_path=sql_path) == 0  # 재실행 안전
    assert init_db.run(config, "aws", sql_path=sql_path) == 0
    with dbmod.connect(config.connection("aws", "admin")) as conn:
        # 대상은 sink 만 쓰고 앱 계정은 닫힌 채 시작한다
        assert not dbmod.has_table_privilege(conn, "migration_app", "public.migration_demo", "INSERT")
        assert dbmod.has_table_privilege(conn, "migration_sink", "public.migration_demo", "UPDATE")
    with dbmod.connect(config.connection("onprem", "admin")) as conn:
        assert dbmod.table_exists(conn, "public.migration_demo")
        assert dbmod.has_table_privilege(conn, "migration_app", "public.migration_demo", "INSERT")
        assert not dbmod.has_table_privilege(conn, "migration_app", "public.migration_demo", "DELETE")
        assert not dbmod.has_table_privilege(conn, "migration_verify", "public.migration_demo", "INSERT")
        with conn.cursor() as cur:
            cur.execute("SELECT rolreplication, rolsuper FROM pg_roles WHERE rolname = 'migration_cdc'")
            repl, superuser = cur.fetchone()
        assert repl and not superuser
    # 앱 계정으로 실제 로그인 가능
    with dbmod.connect(config.connection("onprem", "app")) as conn:
        assert dbmod.server_identity(conn).user == "migration_app"


@pytest.mark.parametrize('count', [800, 1000])
def test_seed_default_demo_and_legacy_counts_and_rerun(patched_env, state_dir, count):
    config = patched_env
    if count == 800:
        config = replace(config, cutover=replace(config.cutover, demo_total_rows=1000))
    assert seed.run(config, "onprem", state_dir=state_dir) == 0
    assert seed.run(config, "onprem", state_dir=state_dir) == 0
    with dbmod.connect(config.connection("onprem", "verify")) as conn:
        assert dbmod.count_rows(conn, "public.migration_demo", "seed") == count
    store = RequestStore(state_dir)
    assert len(store.committed_ids("onprem")) == count
    store.close()


def test_pg_backend_insert_conflict_and_mismatch(patched_env):
    config = patched_env
    backend = PgBackend(config.connection("onprem", "app"))
    rec = RequestRecord(request_id=str(uuid.uuid4()), run_id="it", phase="onprem_live", request_no=1,
                        payload="hello", created_at="2026-02-01T00:00:00.123456+00:00")
    assert backend.insert(rec).outcome == Outcome.INSERTED
    assert backend.insert(rec).outcome == Outcome.EXISTS_EQUAL
    changed = RequestRecord(**{**rec.__dict__, "payload": "other"})
    assert backend.insert(changed).outcome == Outcome.EXISTS_DIFFERENT
    assert backend.lookup(rec.request_id) is not None
    assert backend.identity()["database"] == config.target("onprem").dbname
    backend.close()


def test_fence_revoke_terminate_and_rejection(patched_env):
    config = patched_env
    port = PgAdminPort(config)
    app_conn = dbmod.connect(config.connection("onprem", "app"))  # 기존 세션
    assert port.app_can_insert("onprem")
    port.revoke_app_insert("onprem")
    assert port.terminate_app_sessions("onprem") >= 1
    with pytest.raises(psycopg.Error):
        app_conn.execute("SELECT 1")  # 기존 세션 종료됨
    assert port.test_insert_rejected("onprem", str(uuid.uuid4())) is True
    with dbmod.connect(config.connection("onprem", "verify")) as conn:
        assert dbmod.count_rows(conn, "public.migration_demo", "fence_test") == 0
    port.grant_app_insert("onprem")
    assert port.test_insert_rejected("onprem", str(uuid.uuid4())) is False  # 차단 해제 후, 시험 행은 롤백
    with dbmod.connect(config.connection("onprem", "verify")) as conn:
        assert dbmod.count_rows(conn, "public.migration_demo", "fence_test") == 0


def test_prepare_publication_and_slot_idempotent(patched_env):
    config = patched_env
    with dbmod.connect(config.connection("onprem", "admin")) as conn:
        first = prepare_source(conn, publication="it_pub", slot="it_slot", table="public.migration_demo",
                               owner="migration_cdc")
        second = prepare_source(conn, publication="it_pub", slot="it_slot", table="public.migration_demo",
                                owner="migration_cdc")
        assert first["publication_created"] and first["slot_created"]
        assert not second["publication_created"] and not second["slot_created"]
        assert drop_slot(conn, "it_slot") is True and drop_slot(conn, "it_slot") is False


def test_verify_detects_missing_rows_between_two_dbs(patched_env, state_dir, tmp_path):
    config = patched_env
    seed.run(config, "onprem", state_dir=state_dir, count=1000)
    init_db.run(config, "aws", sql_path=Path(__file__).resolve().parent.parent / "sql" / "001_demo.sql", app_write="allow")
    seed.run(config, "aws", state_dir=None, count=999)
    cmp = verify.run(config, source="onprem", target="aws", state_dir=state_dir, mode="pre",
                     report_path=tmp_path / "r.json")
    assert not cmp.passed
    assert cmp.counts["B_minus_T"] >= 1 and cmp.counts["S_minus_T"] >= 1
    assert (tmp_path / "r.json").exists()
    seed.run(config, "aws", state_dir=None, count=1000)
    # 원본에는 live 행이 더 있어 B-T 가 남지만, 전부 복사된 seed 는 필드까지 일치
    cmp2 = verify.run(config, source="onprem", target="aws", state_dir=state_dir, mode="pre", report_path=None)
    assert cmp2.counts["field_mismatch"] == 0 and cmp2.counts["T_minus_B"] == 0
    # 비밀번호가 보고서에 없어야 한다
    text = (tmp_path / "r.json").read_text()
    assert all(pw not in text for pw in PASSWORDS.values())


def test_watermark_scoped_verify_and_digest_on_real_postgres(patched_env, state_dir, tmp_path):
    """기준점 이전 집합은 digest(행 전송 없음)로, 기준점 이후 delta 는 행 단위로 비교한다."""
    from datetime import timedelta
    config = patched_env
    sql_path = Path(__file__).resolve().parent.parent / "sql" / "001_demo.sql"
    init_db.run(config, "onprem", sql_path=sql_path, app_write="allow")
    init_db.run(config, "aws", sql_path=sql_path, app_write="allow")
    seed.run(config, "onprem", state_dir=state_dir, count=1000)
    seed.run(config, "aws", state_dir=None, count=1000)
    # 앞 테스트가 같은 DB 에 남긴 행이 있을 수 있으므로 먼저 대상을 원본과 같게 맞춘다 (T = B)
    with dbmod.connect(config.connection("onprem", "admin")) as sconn, \
            dbmod.connect(config.connection("aws", "admin")) as tconn:
        src_rows = verify.load_rows(sconn, "public.migration_demo")
        tgt_rows = verify.load_rows(tconn, "public.migration_demo")
        cur = tconn.cursor()
        for rid in set(tgt_rows) - set(src_rows):
            cur.execute("DELETE FROM public.migration_demo WHERE request_id = %s", (rid,))
        for rid in set(src_rows) - set(tgt_rows):
            run_id, phase, no, payload, created = src_rows[rid]
            cur.execute("INSERT INTO public.migration_demo (request_id, run_id, phase, request_no, payload, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s)", (rid, run_id, phase, no, payload, created))
        tconn.commit()
    port = PgAdminPort(config)
    try:
        wm = port.max_created_at("onprem")
        assert wm is not None and wm.endswith("+00:00")
        assert port.count_rows_in_scope("onprem", upto=wm) >= 1000
        assert port.count_rows_in_scope("onprem", since=wm) == 0

        # 1) 스냅샷 집합: digest 가 모두 같으면 행을 읽지 않고 통과. S(ledger 의 seed 1000건) 도 묶음 조회로 확인.
        snap = verify.run(config, source="onprem", target="aws", state_dir=state_dir, mode="pre",
                          report_path=tmp_path / "snap.json", upto=wm, method="digest")
        assert snap.passed and snap.method == "digest" and snap.mismatched_buckets == []
        assert snap.committed_count >= 1000 and snap.source_count == snap.target_count >= 1000
        report = (tmp_path / "snap.json").read_text()
        assert '"method": "digest"' in report and all(pw not in report for pw in PASSWORDS.values())

        # 2) delta: 기준점 뒤 행 3건을 원본에, 2건만 대상에 넣는다 → B-T=1, 스냅샷 집합 판정은 영향 없음
        base = verify.parse_ts(wm)
        delta_rows = [(str(uuid.uuid4()), "live", "onprem_live", n, f"live-{n}", base + timedelta(seconds=n))
                      for n in range(1, 4)]
        for name, rows in (("onprem", delta_rows), ("aws", delta_rows[:2])):
            with dbmod.connect(config.connection(name, "admin")) as conn:
                conn.cursor().executemany(
                    "INSERT INTO public.migration_demo (request_id, run_id, phase, request_no, payload, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)", rows)
                conn.commit()
        assert port.count_rows_in_scope("onprem", since=wm) == 3 and port.count_rows_in_scope("aws", since=wm) == 2
        assert set(port.latest_ids("onprem", 10, since=wm)) == {r[0] for r in delta_rows}
        delta = verify.run(config, source="onprem", target="aws", state_dir=state_dir, mode="pre",
                           report_path=None, since=wm, method="rows")
        assert delta.source_count == 3 and delta.target_count == 2
        assert delta.counts == {"B_minus_T": 1, "T_minus_B": 0, "field_mismatch": 0, "S_minus_T": 0}
        assert delta.missing_in_target == [delta_rows[2][0]]
        again = verify.run(config, source="onprem", target="aws", state_dir=state_dir, mode="pre",
                           report_path=None, upto=wm, method="digest")
        assert again.passed and again.source_count == snap.source_count

        # 3) 대상의 seed 행 하나를 바꾸고 하나를 지우면 digest 가 그 버킷만 집어 행을 읽어 ID 를 보고한다
        with dbmod.connect(config.connection("aws", "admin")) as conn:
            cur = conn.cursor()
            cur.execute("SELECT request_id::text FROM public.migration_demo WHERE phase = 'seed' "
                        "ORDER BY request_no LIMIT 2")
            changed, deleted = [r[0] for r in cur.fetchall()]
            cur.execute("UPDATE public.migration_demo SET payload = 'tampered' WHERE request_id = %s", (changed,))
            cur.execute("DELETE FROM public.migration_demo WHERE request_id = %s", (deleted,))
            conn.commit()
        bad = verify.run(config, source="onprem", target="aws", state_dir=state_dir, mode="pre",
                         report_path=None, upto=wm, method="digest")
        assert not bad.passed
        assert set(bad.mismatched_buckets) == {changed[:2], deleted[:2]}
        assert [d["request_id"] for d in bad.field_mismatch] == [changed]
        assert bad.field_mismatch[0]["fields"]["payload"]["target"] == "tampered"
        assert bad.missing_in_target == [deleted] and bad.committed_missing_in_target == [deleted]
        # 지운 행이 delta 집합 판정에는 섞이지 않는다
        delta2 = verify.run(config, source="onprem", target="aws", state_dir=state_dir, mode="pre",
                            report_path=None, since=wm, method="rows")
        assert delta2.counts == delta.counts
    finally:
        port.close()
