"""원본 B, 대상 T, 커밋 확인 집합 S 를 비교해 JSON 보고서를 만든다. 건수·lag 만으로 통과시키지 않는다.

범위(scope): 마이그레이션 시작 때 저장한 기준점(원본 max(created_at)) 으로 행을 둘로 나눈다.
- 스냅샷 집합 `created_at <= 기준점`: 초기 복사 대상. 한 번 digest 방식으로 확정하고 다시 읽지 않는다.
- delta 집합 `created_at > 기준점`: CDC 로 넘어온 행. 전환 구간의 모든 판정은 이 집합만 행 단위로 비교한다.
행은 INSERT 전용·불변이므로(UPDATE/DELETE 없음) 한 번 일치한 집합은 그대로 유지된다.

방식(method):
- rows: 범위의 행을 양쪽에서 읽어 request_id 집합과 모든 필드를 비교한다 (delta 용).
- digest: 양쪽 서버에서 버킷별 건수·md5 를 계산해 비교하고, 다른 버킷만 행을 읽어 차이 ID 를 보고한다 (스냅샷 집합 용).
  건수만 맞는 것은 통과가 아니다. digest 는 모든 필드의 해시이며, 불일치 버킷은 실제 행을 읽어 ID 를 남긴다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import psycopg
from psycopg import sql

from . import db as dbmod
from .config import Config, ConfigError, load_config
from .state import RequestStore

log = logging.getLogger("migration.verify")

SAMPLE_LIMIT = 20
FIELDS: tuple[str, ...] = ("run_id", "phase", "request_no", "payload", "created_at")
Row = tuple[str, str, int, str, str]
METHODS: tuple[str, ...] = ("rows", "digest")


def parse_ts(value: str | datetime | None) -> datetime | None:
    """체크포인트/CLI 의 ISO 문자열 또는 datetime 을 UTC aware datetime 으로."""
    if value is None:
        return None
    ts = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def format_ts(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat(timespec="microseconds")


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


def load_rows(conn: psycopg.Connection[Any], table: str, *, since: datetime | None = None,
              upto: datetime | None = None, buckets: list[str] | None = None) -> dict[str, Row]:
    """범위(그리고 선택적으로 버킷) 안의 행을 읽어 request_id → 정규화 행."""
    where, params = dbmod.scope_clause(since, upto)
    query = sql.SQL("SELECT request_id, run_id, phase, request_no, payload, created_at FROM {}").format(
        dbmod.table_ident(table)) + where
    if buckets is not None:
        query = query + sql.SQL(" AND " if params else " WHERE ") + sql.SQL("substr(request_id::text, 1, 2) = ANY(%s)")
        params = (*params, list(buckets))
    out: dict[str, Row] = {}
    with conn.cursor(name="verify_scan") as cur:
        cur.itersize = 5000
        cur.execute(query, params)
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
    method: str = "rows"
    mismatched_buckets: list[str] = field(default_factory=list)      # digest 방식에서 행을 다시 읽은 버킷

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
            "method": self.method,
            "counts": self.counts,
            "source_count": self.source_count,
            "target_count": self.target_count,
            "committed_count": self.committed_count,
            "allowed_extra_in_target_count": len(self.allowed_extra_in_target),
            "other_writer_in_target_count": len(self.other_writer_in_target),
            "committed_missing_in_source_count": len(self.committed_missing_in_source),
            "mismatched_buckets": self.mismatched_buckets[:SAMPLE_LIMIT],
            "mismatched_bucket_count": len(self.mismatched_buckets),
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


def compare_digests(source: Mapping[str, tuple[int, str]], target: Mapping[str, tuple[int, str]]) -> list[str]:
    """버킷별 (건수, digest) 가 다른 버킷 이름. 한쪽에만 있는 버킷도 포함."""
    return sorted(b for b in set(source) | set(target) if source.get(b) != target.get(b))


def compare_by_digest(
    sconn: psycopg.Connection[Any],
    tconn: psycopg.Connection[Any],
    table: str,
    committed_pages: Iterable[list[str]],
    *,
    since: datetime | None,
    upto: datetime | None,
) -> Comparison:
    """digest 방식: 버킷 digest 가 모두 같으면 B=T 로 확정하고 S-T 만 묶음 조회한다. 다른 버킷은 행을 읽어 비교한다.

    committed_pages 는 ledger 의 S 집합을 페이지(ID 묶음) 단위로 내는 iterable 이다. 페이지마다 대상에 `= ANY(uuid[])` 로
    묻고 빠진 ID 만 남기므로 S 집합 전체를 메모리에 올리지 않는다. digest 가 다른 버킷에 속한 ID 만 따로 모아 행 비교에 넘긴다.
    """
    src_d = dbmod.bucket_digests(sconn, table, since=since, upto=upto)
    tgt_d = dbmod.bucket_digests(tconn, table, since=since, upto=upto)
    sconn.commit(); tconn.commit()
    bad = compare_digests(src_d, tgt_d)
    bad_set = set(bad)
    src_total = sum(n for n, _ in src_d.values())
    tgt_total = sum(n for n, _ in tgt_d.values())

    # S-T: digest 가 같은 버킷의 커밋 요청은 대상에 있어야 한다 (B=T 이므로 S-T = S-B). 행을 읽지 않고 ID 묶음으로 확인한다.
    committed_count = 0
    committed_in_bad: set[str] = set()
    missing: set[str] = set()
    for page in committed_pages:
        committed_count += len(page)
        rest = [rid for rid in page if rid[:2] not in bad_set]
        committed_in_bad.update(rid for rid in page if rid[:2] in bad_set)
        if rest:
            missing |= dbmod.missing_ids(tconn, table, rest)
    tconn.commit()

    if bad:
        log.info("digest 불일치 버킷 %d/%d: 해당 버킷 행만 다시 읽는다", len(bad), max(len(src_d), len(tgt_d)))
        src_rows = load_rows(sconn, table, since=since, upto=upto, buckets=bad)
        tgt_rows = load_rows(tconn, table, since=since, upto=upto, buckets=bad)
        cmp = compare(src_rows, tgt_rows, committed_in_bad)
        cmp.source_count, cmp.target_count = src_total, tgt_total
    else:
        cmp = Comparison(source_count=src_total, target_count=tgt_total, committed_count=committed_count)
    cmp.committed_count = committed_count
    cmp.method = "digest"
    cmp.mismatched_buckets = bad
    if missing:
        cmp.committed_missing_in_target = sorted(set(cmp.committed_missing_in_target) | missing)
        cmp.committed_missing_in_source = sorted(set(cmp.committed_missing_in_source) | missing)
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
    since: str | datetime | None = None,
    upto: str | datetime | None = None,
    method: str = "rows",
) -> Comparison:
    """두 DB 의 범위 (since, upto] 를 비교하고 보고서를 쓴다. mode=pre 는 전환 직전, post 는 전환 후 판정."""
    if method not in METHODS:
        raise ConfigError(f"verify method 는 {METHODS} 중 하나다: {method}")
    since_ts, upto_ts = parse_ts(since), parse_ts(upto)
    table = config.job.table
    src_spec = config.connection(source, "verify", application_name="migration-verify")
    tgt_spec = config.connection(target, "verify", application_name="migration-verify")
    # ledger(S 집합)를 DB 보다 먼저 읽는다. 워커가 계속 쓰는 중에 DB 를 먼저 읽으면 그 사이에 커밋된 요청이
    # ledger 에만 있어 S-T 로 잘못 잡힌다(2026-11-05 리허설). 먼저 읽으면 그 사이 커밋은 대상에만 있는
    # 행이 되고, post 모드에서는 other_writer_in_target 으로 보고될 뿐 실패가 아니다.
    # digest 방식은 ledger 를 페이지 단위로 흘려 보내며 대상에 묻는다(S 집합 전체를 메모리에 올리지 않음).
    store = RequestStore(state_dir) if state_dir is not None else None
    if store is None:
        log.warning("--state-dir 없음: S 집합(커밋 확인 요청) 비교를 건너뛴다")
    committed: set[str] = set()
    committed_on_target: set[str] = set()
    try:
        if store is not None and method != "digest":
            committed = store.committed_ids_in_scope(since=since_ts, upto=upto_ts)
            committed_on_target = store.committed_ids_in_scope(target, since=since_ts, upto=upto_ts)

        scope = {"since": format_ts(since_ts), "upto": format_ts(upto_ts), "method": method}
        log.info("원본 %s / 대상 %s 비교 범위 %s", src_spec, tgt_spec, scope)
        with dbmod.connect(src_spec, statement_timeout_ms=600_000) as sconn, \
                dbmod.connect(tgt_spec, statement_timeout_ms=600_000) as tconn:
            src_ident = dbmod.server_identity(sconn).as_dict()
            tgt_ident = dbmod.server_identity(tconn).as_dict()
            if method == "digest":
                if mode == "post":
                    # 전환 후 대상에는 새 행이 있어 digest 가 어긋난다. post 는 delta 를 rows 로 비교하고,
                    # digest 는 기준점 이전(불변) 집합에만 쓴다.
                    raise ConfigError("digest 방식은 pre 모드(기준점 이전 집합)에만 쓴다")
                pages: Iterable[list[str]] = (store.iter_committed_ids_in_scope(since=since_ts, upto=upto_ts)
                                              if store is not None else ())
                cmp = compare_by_digest(sconn, tconn, table, pages, since=since_ts, upto=upto_ts)
            else:
                src_rows = load_rows(sconn, table, since=since_ts, upto=upto_ts)
                tgt_rows = load_rows(tconn, table, since=since_ts, upto=upto_ts)
                allowed_extra = committed_on_target if mode == "post" else set()
                cmp = compare(src_rows, tgt_rows, committed, allowed_extra=allowed_extra,
                              tolerate_other_writers=(mode == "post"))
    finally:
        if store is not None:
            store.close()

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "job_id": config.job.job_id,
        "mode": mode,
        "scope": scope,
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
    log.info("판정 %s (%s %s): counts=%s source=%d target=%d committed=%d allowed_extra=%d other_writer=%d",
             "PASS" if cmp.passed else "FAIL", method, scope, cmp.counts, cmp.source_count, cmp.target_count,
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
    p.add_argument("--since", default=None, help="기준점(ISO). 이 시각 뒤에 만들어진 행만 비교 (delta)")
    p.add_argument("--upto", default=None, help="이 시각까지(포함) 만들어진 행만 비교 (스냅샷 집합)")
    p.add_argument("--method", choices=METHODS, default="rows",
                   help="rows: 행 전체 비교. digest: 서버측 버킷 해시 비교 후 불일치 버킷만 행 비교 (pre 전용)")
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
            since=args.since,
            upto=args.upto,
            method=args.method,
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
