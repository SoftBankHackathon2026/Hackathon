"""seed 1,000건을 결정적 request_id(uuid5)로 삽입하고 정확히 1,000건인지 확인한다."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg import sql

from . import db as dbmod
from .config import Config, ConfigError, load_config
from .state import RequestRecord, RequestStore

log = logging.getLogger("migration.seed")

# 고정 네임스페이스: 양쪽 DB·재실행에서 같은 ID 가 나오게 한다. 바꾸면 seed 집합이 달라진다.
SEED_NAMESPACE = uuid.UUID("5f3c1c2e-7b7a-4e3f-9f4e-2c1a0b9d8e7f")
SEED_PHASE = "seed"
SEED_RUN_ID = "seed"
SEED_COUNT = 1000
# created_at 도 결정적으로: 기준 시각 + n초
SEED_BASE_TIME = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def seed_request_id(n: int) -> uuid.UUID:
    """request_no n 의 결정적 request_id."""
    return uuid.uuid5(SEED_NAMESPACE, f"seed-{n}")


def seed_records(count: int = SEED_COUNT, run_id: str = SEED_RUN_ID) -> list[RequestRecord]:
    """request_no 1..count 의 seed 레코드. 모든 필드가 결정적이다."""
    out: list[RequestRecord] = []
    for n in range(1, count + 1):
        created = SEED_BASE_TIME + timedelta(seconds=n)
        out.append(RequestRecord(
            request_id=str(seed_request_id(n)),
            run_id=run_id,
            phase=SEED_PHASE,
            request_no=n,
            payload=f"seed payload {n}",
            created_at=created.isoformat(timespec="microseconds"),
        ))
    return out


def insert_seed(conn: psycopg.Connection[Any], table: str, records: Iterable[RequestRecord], batch: int = 200) -> int:
    """ON CONFLICT DO NOTHING 으로 삽입. 새로 들어간 건수를 돌려준다."""
    inserted = 0
    stmt = sql.SQL(
        "INSERT INTO {} (request_id, run_id, phase, request_no, payload, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (request_id) DO NOTHING"
    ).format(dbmod.table_ident(table))
    rows = [(r.request_id, r.run_id, r.phase, r.request_no, r.payload, datetime.fromisoformat(r.created_at))
            for r in records]
    with conn.cursor() as cur:
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            for row in chunk:
                cur.execute(stmt, row)
                inserted += cur.rowcount
            conn.commit()
    return inserted


def verify_seed(conn: psycopg.Connection[Any], table: str, records: list[RequestRecord]) -> list[str]:
    """phase=seed 행이 정확히 기대 집합과 같은지. 문제 목록을 돌려준다 (비어 있으면 통과)."""
    problems: list[str] = []
    expected = {r.request_id: r for r in records}
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT request_id, run_id, request_no, payload, created_at FROM {} WHERE phase = %s")
            .format(dbmod.table_ident(table)),
            (SEED_PHASE,),
        )
        rows = cur.fetchall()
    conn.commit()
    actual_ids = {str(r[0]) for r in rows}
    if len(rows) != len(records):
        problems.append(f"seed 건수 {len(rows)} != {len(records)}")
    missing = set(expected) - actual_ids
    extra = actual_ids - set(expected)
    if missing:
        problems.append(f"누락 {len(missing)}건 예: {sorted(missing)[:5]}")
    if extra:
        problems.append(f"예상 밖 seed 행 {len(extra)}건 예: {sorted(extra)[:5]}")
    for rid, run_id, no, payload, created_at in rows:
        exp = expected.get(str(rid))
        if exp is None:
            continue
        got = (run_id, int(no), payload, created_at.astimezone(UTC).isoformat(timespec="microseconds"))
        want = (exp.run_id, exp.request_no, exp.payload, exp.created_at)
        if got != want:
            problems.append(f"필드 불일치 {rid}: {got} != {want}")
    return problems


def run(config: Config, target: str, *, state_dir: Path | None, count: int | None = None) -> int:
    demo = config.cutover.demo_total_rows is not None
    count = (800 if demo else SEED_COUNT) if count is None else count
    if demo and count != 800:
        raise ConfigError("1000-row demo requires exactly 800 seed rows")
    records = seed_records(count)
    spec = config.connection(target, "app", application_name="migration-seed")
    log.info("seed 삽입 대상 %s, %d건", spec, count)
    with dbmod.connect(spec) as conn:
        ident = dbmod.server_identity(conn)
        log.info("접속 확인 db=%s addr=%s user=%s", ident.database, ident.server_addr, ident.user)
        inserted = insert_seed(conn, config.job.table, records)
        log.info("새로 삽입 %d건 (나머지는 이미 존재)", inserted)
        problems = verify_seed(conn, config.job.table, records)
        total = dbmod.count_rows(conn, config.job.table, SEED_PHASE)
    if state_dir is not None:
        store = RequestStore(state_dir)
        try:
            for rec in records:
                store.record_pending(rec)
                store.mark_committed(rec.request_id, target, 1)
        finally:
            store.close()
        log.info("요청 기록(SQLite)에 seed %d건을 committed 로 남김: %s", count, state_dir)
    if problems:
        for p in problems:
            log.error("seed 검증 실패: %s", p)
        return 1
    log.info("seed 검증 통과: phase=%s 행 수 = %d", SEED_PHASE, total)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="seed 1,000건 결정적 삽입")
    p.add_argument("--config", default=os.environ.get("MIGRATION_CONFIG", "config.yaml"))
    p.add_argument("--target", default=None, help="기본: job.source")
    p.add_argument("--count", type=int, default=None, help="기본 1000; demo_total_rows=1000이면 800")
    p.add_argument("--state-dir", default=os.environ.get("WORKER_STATE_DIR"),
                   help="지정하면 seed 를 요청 기록에도 committed 로 남긴다 (verify 의 S 집합에 포함)")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = load_config(args.config)
        target = args.target or config.job.source
        return run(config, target, state_dir=Path(args.state_dir) if args.state_dir else None, count=args.count)
    except ConfigError as exc:
        log.error("설정 오류: %s", exc)
        return 2
    except psycopg.Error as exc:
        log.error("DB 오류: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
