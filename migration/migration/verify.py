"""원본 B, 대상 T 전체 행과 커밋 확인 집합 S 를 비교해 JSON 보고서를 만든다. 건수·checksum 만으로 통과시키지 않는다."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import psycopg
from psycopg import sql

from . import db as dbmod
from .config import Config, ConfigError, load_config
from .state import RequestStore

log = logging.getLogger("migration.verify")

SAMPLE_LIMIT = 20
FIELDS: tuple[str, ...] = ("run_id", "phase", "request_no", "payload", "created_at")
Row = tuple[str, str, int, str, str]


def normalize_row(row: tuple[Any, ...]) -> Row:
    """DB 행을 (run_id, phase, request_no, payload, created_at[UTC ISO]) 로 정규화한다."""
    run_id, phase, request_no, payload, created_at = row
    if isinstance(created_at, datetime):
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        created_at = created_at.astimezone(UTC).isoformat(timespec="microseconds")
    else:
        created_at = datetime.fromisoformat(str(created_at)).astimezone(UTC).isoformat(timespec="microseconds")
    return (str(run_id), str(phase), int(request_no), str(payload), created_at)


def load_rows(conn: psycopg.Connection[Any], table: str) -> dict[str, Row]:
    """전체 행을 읽어 request_id → 정규화 행."""
    out: dict[str, Row] = {}
    with conn.cursor(name="verify_scan") as cur:
        cur.itersize = 5000
        cur.execute(
            sql.SQL("SELECT request_id, run_id, phase, request_no, payload, created_at FROM {}")
            .format(dbmod.table_ident(table))
        )
        for row in cur:
            out[str(row[0])] = normalize_row(tuple(row[1:]))
    conn.commit()
    return out


@dataclass
class Comparison:
    """비교 결과. 모든 count 가 0 이면 통과."""

    source_count: int
    target_count: int
    committed_count: int
    missing_in_target: list[str] = field(default_factory=list)      # B - T
    unexpected_in_target: list[str] = field(default_factory=list)   # T - B (허용 집합 제외)
    allowed_extra_in_target: list[str] = field(default_factory=list)  # 전환 후 T-B 중 S(target) 에 있는 것
    other_writer_in_target: list[str] = field(default_factory=list)  # post: 대상에만 있고 워커 ledger 에도 없는 것 (앱 등 다른 writer). 실패 아님
    field_mismatch: list[dict[str, Any]] = field(default_factory=list)
    committed_missing_in_target: list[str] = field(default_factory=list)  # S - T
    committed_missing_in_source: list[str] = field(default_factory=list)  # S(source) - B, 참고용

    @property
    def counts(self) -> dict[str, int]:
        return {
            "B_minus_T": len(self.missing_in_target),
            "T_minus_B": len(self.unexpected_in_target),
            "field_mismatch": len(self.field_mismatch),
            "S_minus_T": len(self.committed_missing_in_target),
        }

    @property
    def passed(self) -> bool:
        return all(v == 0 for v in self.counts.values())

    def as_report(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "counts": self.counts,
            "source_count": self.source_count,
            "target_count": self.target_count,
            "committed_count": self.committed_count,
            "allowed_extra_in_target_count": len(self.allowed_extra_in_target),
            "other_writer_in_target_count": len(self.other_writer_in_target),
            "committed_missing_in_source_count": len(self.committed_missing_in_source),
            "samples": {
                "B_minus_T": sorted(self.missing_in_target)[:SAMPLE_LIMIT],
                "T_minus_B": sorted(self.unexpected_in_target)[:SAMPLE_LIMIT],
                "field_mismatch": self.field_mismatch[:SAMPLE_LIMIT],
                "S_minus_T": sorted(self.committed_missing_in_target)[:SAMPLE_LIMIT],
                "allowed_extra_in_target": sorted(self.allowed_extra_in_target)[:SAMPLE_LIMIT],
                "other_writer_in_target": sorted(self.other_writer_in_target)[:SAMPLE_LIMIT],
                "committed_missing_in_source": sorted(self.committed_missing_in_source)[:SAMPLE_LIMIT],
            },
        }


def compare(
    source: Mapping[str, Row],
    target: Mapping[str, Row],
    committed: set[str],
    *,
    allowed_extra: set[str] | None = None,
    tolerate_other_writers: bool = False,
) -> Comparison:
    """B/T/S 집합 판정. allowed_extra 는 전환 후 대상에서만 커밋된 요청 ID (T-B 로 세지 않는다).

    tolerate_other_writers (post 모드): 원본은 이미 fencing 돼 있으므로 대상에만 있는 행은 전환 뒤 다른 writer
    (데모 앱·트래픽 봇 등)가 쓴 것이다. 실패로 세지 않고 other_writer_in_target 으로만 보고한다.
    """
    allowed_extra = allowed_extra or set()
    cmp = Comparison(source_count=len(source), target_count=len(target), committed_count=len(committed))
    src_ids, tgt_ids = set(source), set(target)
    cmp.missing_in_target = sorted(src_ids - tgt_ids)
    extra = tgt_ids - src_ids
    cmp.allowed_extra_in_target = sorted(extra & allowed_extra)
    if tolerate_other_writers:
        cmp.other_writer_in_target = sorted(extra - allowed_extra)
    else:
        cmp.unexpected_in_target = sorted(extra - allowed_extra)
    for rid in src_ids & tgt_ids:
        s, t = source[rid], target[rid]
        if s != t:
            diff = {name: {"source": sv, "target": tv} for name, sv, tv in zip(FIELDS, s, t, strict=True) if sv != tv}
            cmp.field_mismatch.append({"request_id": rid, "fields": diff})
    cmp.field_mismatch.sort(key=lambda d: d["request_id"])
    cmp.committed_missing_in_target = sorted(committed - tgt_ids)
    cmp.committed_missing_in_source = sorted((committed - allowed_extra) - src_ids)
    return cmp


def run(
    config: Config,
    *,
    source: str,
    target: str,
    state_dir: Path | None,
    mode: str,
    report_path: Path | None,
    extra_meta: Mapping[str, Any] | None = None,
) -> Comparison:
    """두 DB 를 읽어 비교하고 보고서를 쓴다. mode=pre 는 전환 직전, post 는 전환 후 판정."""
    table = config.job.table
    src_spec = config.connection(source, "verify", application_name="migration-verify")
    tgt_spec = config.connection(target, "verify", application_name="migration-verify")
    # ledger(S 집합)를 DB 보다 먼저 읽는다. 워커가 계속 쓰는 중에 DB 를 먼저 읽으면 그 사이에 커밋된 요청이
    # ledger 에만 있어 S-T 로 잘못 잡힌다(2026-11-05 리허설). 먼저 읽으면 그 사이 커밋은 대상에만 있는
    # 행이 되고, post 모드에서는 other_writer_in_target 으로 보고될 뿐 실패가 아니다.
    committed: set[str] = set()
    committed_on_target: set[str] = set()
    if state_dir is not None:
        store = RequestStore(state_dir)
        try:
            committed = store.committed_ids()
            committed_on_target = store.committed_ids(target)
        finally:
            store.close()
    else:
        log.warning("--state-dir 없음: S 집합(커밋 확인 요청) 비교를 건너뛴다")

    log.info("원본 %s / 대상 %s 전체 행 읽기", src_spec, tgt_spec)
    with dbmod.connect(src_spec, statement_timeout_ms=600_000) as sconn:
        src_ident = dbmod.server_identity(sconn).as_dict()
        src_rows = load_rows(sconn, table)
    with dbmod.connect(tgt_spec, statement_timeout_ms=600_000) as tconn:
        tgt_ident = dbmod.server_identity(tconn).as_dict()
        tgt_rows = load_rows(tconn, table)

    allowed_extra = committed_on_target if mode == "post" else set()
    cmp = compare(src_rows, tgt_rows, committed, allowed_extra=allowed_extra, tolerate_other_writers=(mode == "post"))
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "job_id": config.job.job_id,
        "mode": mode,
        "source": {"name": source, **src_ident},
        "target": {"name": target, **tgt_ident},
        "table": table,
        **(dict(extra_meta) if extra_meta else {}),
        **cmp.as_report(),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(text, encoding="utf-8")
        log.info("보고서 저장: %s", report_path)
    log.info("판정 %s: counts=%s source=%d target=%d committed=%d allowed_extra=%d other_writer=%d",
             "PASS" if cmp.passed else "FAIL", cmp.counts, cmp.source_count, cmp.target_count,
             cmp.committed_count, len(cmp.allowed_extra_in_target), len(cmp.other_writer_in_target))
    return cmp


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="B/T/S 집합 비교 검증")
    p.add_argument("--config", default=os.environ.get("MIGRATION_CONFIG", "config.yaml"))
    p.add_argument("--source", default=None, help="기본: job.source")
    p.add_argument("--target", default=None, help="기본: job.target")
    p.add_argument("--state-dir", default=os.environ.get("WORKER_STATE_DIR"))
    p.add_argument("--mode", choices=("pre", "post"), default="pre",
                   help="pre: T-B 는 0 이어야 함. post: 대상에서 커밋 확인된 요청은 T-B 에서 제외")
    p.add_argument("--report", default=None, help="JSON 보고서 경로")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = load_config(args.config)
        cmp = run(
            config,
            source=args.source or config.job.source,
            target=args.target or config.job.target,
            state_dir=Path(args.state_dir) if args.state_dir else None,
            mode=args.mode,
            report_path=Path(args.report) if args.report else None,
        )
    except ConfigError as exc:
        log.error("설정 오류: %s", exc)
        return 2
    except psycopg.Error as exc:
        log.error("DB 오류: %s", exc)
        return 2
    print(json.dumps(cmp.as_report(), ensure_ascii=False, indent=2))
    return 0 if cmp.passed else 1


if __name__ == "__main__":
    sys.exit(main())
