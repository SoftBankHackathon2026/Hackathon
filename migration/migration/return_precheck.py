"""AWS → 온프렘 귀환 사전 점검. 읽기 전용이다. DB·커넥터·권한을 바꾸지 않는다.

확인하는 것 (귀환 카드 "원본 점검·귀환 대상 준비" 완료 기준):
- 귀환 원본은 현재 AWS 다: 논리 디코딩(wal_level=logical), CDC 계정 복제 권한, 정방향 이후 데이터 유지(초기화 안 됨).
- 귀환 대상은 빈 온프렘 DB 다: 옛 정방향 원본과 다른 DB, 테이블 비어 있음, 앱 INSERT 닫힘, sink 쓰기 가능.
- 두 DB 의 테이블 컬럼·타입·확장이 같다.
- 정방향 Source/Sink 커넥터가 남아 있지 않다 (자동 재개 방지).

사용:
  python -m migration.return_precheck --config config.return.yaml \
      [--forward-config config.yaml] [--skip-connect] [--report reports/return-precheck.json]

종료 코드: 0 모든 필수 항목 통과, 1 실패 항목 있음, 2 설정 오류.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import psycopg

from . import db as dbmod
from .cdc import ConnectClient, ConnectError
from .config import Config, ConfigError, load_config, redact

log = logging.getLogger("migration.return_precheck")

SEED_ROWS = 1000
SETTINGS = ("wal_level", "rds.logical_replication", "max_replication_slots", "max_wal_senders", "server_version_num")


@dataclass
class Check:
    name: str
    ok: bool | None  # None: 판정 불가(정보·건너뜀). 필수 실패만 False.
    detail: str
    required: bool = True


# ---- 사실 수집 (DB 읽기만) ------------------------------------------------------------


def _settings(conn: psycopg.Connection[Any]) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name, setting FROM pg_settings WHERE name = ANY(%s)", (list(SETTINGS),))
        return {str(n): str(s) for n, s in cur.fetchall()}


def _columns(conn: psycopg.Connection[Any], table: str) -> list[dict[str, Any]]:
    schema, name = dbmod.split_table(table)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
            "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum",
            (schema, name),
        )
        return [{"name": r[0], "type": r[1], "not_null": bool(r[2])} for r in cur.fetchall()]


def _primary_key(conn: psycopg.Connection[Any], table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = to_regclass(%s) AND i.indisprimary ORDER BY a.attnum",
            (table,),
        )
        return [r[0] for r in cur.fetchall()]


def _extensions(conn: psycopg.Connection[Any]) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT extname, extversion FROM pg_extension ORDER BY extname")
        return {r[0]: r[1] for r in cur.fetchall()}


def _phase_counts(conn: psycopg.Connection[Any], table: str) -> dict[str, int]:
    from psycopg import sql

    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT phase, count(*) FROM {} GROUP BY phase").format(dbmod.table_ident(table)))
        return {str(r[0]): int(r[1]) for r in cur.fetchall()}


def _role_replication(conn: psycopg.Connection[Any], role: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT rolreplication FROM pg_roles WHERE rolname = %s", (role,))
        row = cur.fetchone()
        if row is None:
            return {"exists": False, "rolreplication": False, "rds_replication_member": False}
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'rds_replication'")
        member = False
        if cur.fetchone():
            cur.execute("SELECT pg_has_role(%s, 'rds_replication', 'MEMBER')", (role,))
            member = bool(cur.fetchone()[0])
        return {"exists": True, "rolreplication": bool(row[0]), "rds_replication_member": member}


def _slots(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT slot_name, plugin, database, active FROM pg_replication_slots ORDER BY slot_name")
        return [{"slot": r[0], "plugin": r[1], "database": r[2], "active": bool(r[3])} for r in cur.fetchall()]


def _publications(conn: psycopg.Connection[Any]) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT pubname FROM pg_publication ORDER BY pubname")
        return [r[0] for r in cur.fetchall()]


def _database_exists(conn: psycopg.Connection[Any], dbname: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        return cur.fetchone() is not None


def _table_facts(conn: psycopg.Connection[Any], config: Config, target_name: str) -> dict[str, Any]:
    table = config.job.table
    tgt = config.target(target_name)
    facts: dict[str, Any] = {
        "identity": dbmod.server_identity(conn).as_dict(),
        "settings": _settings(conn),
        "extensions": _extensions(conn),
        "table_exists": dbmod.table_exists(conn, table),
        "cdc_role": _role_replication(conn, tgt.user_spec("cdc").user),
        "slots": _slots(conn),
        "publications": _publications(conn),
    }
    if facts["table_exists"]:
        facts["columns"] = _columns(conn, table)
        facts["primary_key"] = _primary_key(conn, table)
        facts["row_count"] = dbmod.count_rows(conn, table)
        facts["phase_counts"] = _phase_counts(conn, table)
        facts["privileges"] = {
            role: {p: dbmod.has_table_privilege(conn, tgt.user_spec(role).user, table, p)
                   for p in ("SELECT", "INSERT", "UPDATE")}
            for role in ("app", "cdc", "sink", "verify")
        }
    conn.commit()
    return facts


def gather_db(config: Config, target_name: str) -> dict[str, Any]:
    """target 의 관리자 계정으로 읽기 전용 사실을 모은다. DB 가 없으면 database_exists=False 만 돌려준다."""
    tgt = config.target(target_name)
    maint = config.connection(target_name, "admin", dbname=tgt.admin_dbname, application_name="migration-return-precheck")
    with dbmod.connect(maint) as conn:
        exists = _database_exists(conn, tgt.dbname)
        conn.commit()
    if not exists:
        return {"database_exists": False, "dbname": tgt.dbname}
    spec = config.connection(target_name, "admin", application_name="migration-return-precheck")
    with dbmod.connect(spec) as conn:
        facts = _table_facts(conn, config, target_name)
    facts["database_exists"] = True
    facts["dbname"] = tgt.dbname
    return facts


def gather_forward_slot(forward: Config) -> dict[str, Any]:
    """정방향 원본(옛 온프렘)에 남은 정방향 슬롯 상태. 귀환 중에도 WAL 이 쌓이는지 확인용."""
    spec = forward.connection(forward.job.source, "admin", application_name="migration-return-precheck")
    with dbmod.connect(spec) as conn:
        slots = [s for s in _slots(conn) if s["slot"] == forward.job.slot_name]
        conn.commit()
    return {"slot_name": forward.job.slot_name, "slot": slots[0] if slots else None}


def gather_connect(config: Config, forward_job_id: str) -> dict[str, Any]:
    client = ConnectClient(config.kafka.connect_url)
    names = client.list_connectors()
    out: dict[str, Any] = {"connectors": names, "forward": {}, "return": {}}
    for prefix, key in ((forward_job_id, "forward"), (config.job.job_id, "return")):
        for kind in ("source", "sink"):
            name = f"{prefix}-{kind}"
            if name in names:
                st = client.status(name)
                out[key][name] = (st.get("connector") or {}).get("state")
            else:
                out[key][name] = None
    return out


# ---- 판정 (순수 함수) ------------------------------------------------------------------


def _schema_signature(facts: Mapping[str, Any]) -> list[tuple[str, str, bool]]:
    return [(c["name"], c["type"], c["not_null"]) for c in facts.get("columns") or []]


def evaluate(
    config: Config,
    aws: Mapping[str, Any],
    onprem: Mapping[str, Any],
    *,
    connect: Mapping[str, Any] | None = None,
    forward: Config | None = None,
    forward_slot: Mapping[str, Any] | None = None,
) -> list[Check]:
    checks: list[Check] = []
    src_name, tgt_name = config.job.source, config.job.target

    # 0. 설정 방향
    checks.append(Check("config.direction", src_name == "aws",
                        f"job.source={src_name}, job.target={tgt_name} (귀환은 source=aws)"))
    checks.append(Check("config.job_id_separate", forward is None or forward.job.job_id != config.job.job_id,
                        f"귀환 job_id={config.job.job_id}" + (f", 정방향 job_id={forward.job.job_id}" if forward else "")))

    # 1. 원본 AWS
    s = aws.get("settings") or {}
    checks.append(Check("aws.wal_level_logical", s.get("wal_level") == "logical",
                        f"wal_level={s.get('wal_level')}, rds.logical_replication={s.get('rds.logical_replication', '-')} "
                        "(RDS 는 파라미터 그룹 rds.logical_replication=1 + 재부팅)"))
    slots_max = int(s.get("max_replication_slots") or 0)
    used = len(aws.get("slots") or [])
    checks.append(Check("aws.replication_slot_capacity", slots_max > used,
                        f"max_replication_slots={slots_max}, 사용 중 {used}"))
    cdc = aws.get("cdc_role") or {}
    cdc_ok = bool(cdc.get("rolreplication") or cdc.get("rds_replication_member"))
    checks.append(Check("aws.cdc_replication", cdc_ok,
                        f"migration_cdc exists={cdc.get('exists')}, REPLICATION={cdc.get('rolreplication')}, "
                        f"rds_replication={cdc.get('rds_replication_member')} (sql/return/aws_source_replication.sql)"))
    checks.append(Check("aws.table_exists", bool(aws.get("table_exists")), f"{config.job.table} on aws"))
    phases = aws.get("phase_counts") or {}
    seed_n = int(phases.get("seed", 0))
    total = int(aws.get("row_count") or 0)
    checks.append(Check("aws.seed_preserved", seed_n == SEED_ROWS,
                        f"phase=seed {seed_n}건 (정방향에서 넘어온 {SEED_ROWS}건이 그대로여야 한다)"))
    checks.append(Check("aws.not_reset", total > SEED_ROWS,
                        f"전체 {total}건, phase 별 {phases} (seed 외 데이터가 있어야 초기화되지 않은 현재 AWS)"))
    privs = aws.get("privileges") or {}
    checks.append(Check("aws.app_write_open", bool((privs.get("app") or {}).get("INSERT")),
                        "AWS 는 현재 운영 DB 이므로 귀환 FENCING 전까지 앱 INSERT 가 열려 있어야 한다"))
    checks.append(Check("aws.cdc_select", bool((privs.get("cdc") or {}).get("SELECT")), "스냅샷용 SELECT"))
    pubs = aws.get("publications") or []
    ret_pub, ret_slot = config.job.publication_name, config.job.slot_name
    leftover = [n for n in pubs if n != ret_pub] + [x["slot"] for x in aws.get("slots") or [] if x["slot"] != ret_slot]
    checks.append(Check("aws.no_foreign_cdc_objects", not leftover,
                        f"귀환 외 publication/slot: {leftover or '없음'}", required=False))

    # 2. 대상 온프렘 (빈 귀환 DB)
    if forward is not None:
        f_src = forward.target(forward.job.source)
        r_tgt = config.target(tgt_name)
        same = (f_src.host, f_src.port, f_src.dbname) == (r_tgt.host, r_tgt.port, r_tgt.dbname)
        checks.append(Check("onprem.separate_from_forward_source", not same,
                            f"정방향 원본 {f_src.host}/{f_src.dbname}, 귀환 대상 {r_tgt.host}/{r_tgt.dbname}"))
    else:
        checks.append(Check("onprem.separate_from_forward_source", None,
                            "--forward-config 없음: 옛 온프렘 DB 와 분리됐는지 비교하지 않음", required=False))
    if not onprem.get("database_exists"):
        checks.append(Check("onprem.database_exists", False,
                            f"{onprem.get('dbname')} 없음 → python -m migration.init_db --config <귀환설정> --target {tgt_name}"))
    else:
        checks.append(Check("onprem.database_exists", True, f"{onprem.get('dbname')}"))
        checks.append(Check("onprem.table_exists", bool(onprem.get("table_exists")), f"{config.job.table} on {tgt_name}"))
        if onprem.get("table_exists"):
            n = int(onprem.get("row_count") or 0)
            checks.append(Check("onprem.table_empty", n == 0, f"{n}건 (귀환 대상은 비어 있어야 한다. seed 를 넣지 않는다)"))
            tp = onprem.get("privileges") or {}
            checks.append(Check("onprem.app_write_closed", not (tp.get("app") or {}).get("INSERT"),
                                "대상 앱 INSERT 는 SWITCHING 전까지 닫혀 있어야 한다 (Sink 만 적재)"))
            sink = tp.get("sink") or {}
            checks.append(Check("onprem.sink_write", bool(sink.get("INSERT") and sink.get("UPDATE")),
                                f"migration_sink INSERT={sink.get('INSERT')} UPDATE={sink.get('UPDATE')} (upsert)"))
            checks.append(Check("onprem.verify_select", bool((tp.get("verify") or {}).get("SELECT")), "verify 읽기"))
            # 3. 스키마 비교
            a_sig, o_sig = _schema_signature(aws), _schema_signature(onprem)
            checks.append(Check("schema.columns_match", bool(a_sig) and a_sig == o_sig,
                                "같음" if a_sig == o_sig else f"aws={a_sig} onprem={o_sig}"))
            checks.append(Check("schema.primary_key_match",
                                bool(aws.get("primary_key")) and aws.get("primary_key") == onprem.get("primary_key"),
                                f"aws={aws.get('primary_key')} onprem={onprem.get('primary_key')} (sink upsert 키 request_id)"))
    a_ext, o_ext = aws.get("extensions") or {}, onprem.get("extensions") or {}
    if onprem.get("database_exists"):
        missing = sorted(set(a_ext) - set(o_ext) - {"plpgsql"})
        checks.append(Check("schema.extensions", not missing,
                            f"온프렘에 없는 AWS 확장: {missing or '없음'} (rds_tools 등 RDS 전용은 무시 가능)", required=False))
    a_ver = str((aws.get("settings") or {}).get("server_version_num", ""))[:2]
    o_ver = str((onprem.get("settings") or {}).get("server_version_num", ""))[:2]
    if a_ver and o_ver:
        checks.append(Check("schema.major_version", a_ver == o_ver, f"aws={a_ver} onprem={o_ver}", required=False))

    # 4. 커넥터
    if connect is None:
        checks.append(Check("connect.forward_stopped", None, "--skip-connect: Kafka Connect 를 확인하지 않음"))
    else:
        alive = {k: v for k, v in (connect.get("forward") or {}).items() if v is not None}
        checks.append(Check("connect.forward_stopped", not alive,
                            f"정방향 커넥터 남음: {alive}" if alive else "정방향 Source/Sink 없음"))
        ret = {k: v for k, v in (connect.get("return") or {}).items() if v is not None}
        checks.append(Check("connect.return_not_started", not ret,
                            f"귀환 커넥터가 이미 있음: {ret}" if ret else "귀환 커넥터 미등록 (cutover 가 등록)",
                            required=False))
    if forward_slot is not None:
        slot = forward_slot.get("slot")
        checks.append(Check("forward.slot_inactive", slot is None or not slot.get("active"),
                            f"옛 온프렘 슬롯 {forward_slot.get('slot_name')}: "
                            + ("없음" if slot is None else f"active={slot.get('active')} (남아 있으면 WAL 축적, drop-slot 은 팀 결정)"),
                            required=False))
    return checks


def passed(checks: list[Check]) -> bool:
    return all(c.ok is not False for c in checks if c.required)


# ---- CLI ----------------------------------------------------------------------------


def run(config: Config, *, forward: Config | None, skip_connect: bool, report_path: Path | None) -> int:
    aws = gather_db(config, config.job.source)
    onprem = gather_db(config, config.job.target)
    connect = None
    if not skip_connect:
        try:
            connect = gather_connect(config, forward.job.job_id if forward else "forward_001")
        except ConnectError as exc:
            log.error("Kafka Connect 조회 실패: %s", exc)
            connect = {"forward": {"<조회 실패>": str(exc)}, "return": {}}
    forward_slot = None
    if forward is not None:
        try:
            forward_slot = gather_forward_slot(forward)
        except psycopg.Error as exc:
            log.warning("정방향 원본 슬롯 조회 실패: %s", exc)
    checks = evaluate(config, aws, onprem, connect=connect, forward=forward, forward_slot=forward_slot)
    ok = passed(checks)
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "job_id": config.job.job_id,
        "direction": f"{config.job.source}->{config.job.target}",
        "passed": ok,
        "checks": [asdict(c) for c in checks],
        "facts": {"aws": aws, "onprem": onprem, "connect": connect, "forward_slot": forward_slot},
    }
    text = redact(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(text, encoding="utf-8")
        log.info("보고서 저장: %s", report_path)
    for c in checks:
        mark = {True: "PASS", False: "FAIL" if c.required else "WARN", None: "SKIP"}[c.ok]
        print(f"[{mark}] {c.name}: {c.detail}")
    print("판정:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AWS → 온프렘 귀환 사전 점검 (읽기 전용)")
    p.add_argument("--config", default=os.environ.get("MIGRATION_CONFIG", "config.return.yaml"))
    p.add_argument("--forward-config", default=None, help="정방향 설정. 옛 온프렘 DB 분리·정방향 슬롯 확인용")
    p.add_argument("--skip-connect", action="store_true", help="Kafka Connect 확인 생략")
    p.add_argument("--report", default=None)
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = load_config(args.config)
        forward = load_config(args.forward_config) if args.forward_config else None
        return run(config, forward=forward, skip_connect=args.skip_connect,
                   report_path=Path(args.report) if args.report else None)
    except ConfigError as exc:
        log.error("설정 오류: %s", exc)
        return 2
    except psycopg.Error as exc:
        log.error("DB 오류: %s", redact(str(exc)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
