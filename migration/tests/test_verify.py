"""verify.py: B/T/S 집합 판정과 timestamptz 정규화."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from migration.verify import compare, normalize_row


def _row(n: int, phase: str = "seed", payload: str | None = None):
    return ("seed", phase, n, payload or f"p{n}", f"2026-01-01T00:00:{n:02d}.000000+00:00")


def test_normalize_timestamptz_to_utc():
    kst = timezone(timedelta(hours=9))
    row = ("r", "seed", 1, "p", datetime(2026, 1, 1, 9, 0, 0, tzinfo=kst))
    assert normalize_row(row)[4] == "2026-01-01T00:00:00.000000+00:00"
    naive = ("r", "seed", 1, "p", datetime(2026, 1, 1, 0, 0, 0))
    assert normalize_row(naive)[4] == "2026-01-01T00:00:00.000000+00:00"


def test_compare_passes_when_identical():
    src = {f"id{n}": _row(n) for n in range(1, 6)}
    cmp = compare(src, dict(src), committed={"id1", "id2"})
    assert cmp.passed and cmp.counts == {"B_minus_T": 0, "T_minus_B": 0, "field_mismatch": 0, "S_minus_T": 0}


def test_compare_reports_each_difference():
    src = {f"id{n}": _row(n) for n in range(1, 5)}
    tgt = dict(src)
    del tgt["id4"]                         # B-T
    tgt["extra"] = _row(99)                # T-B
    tgt["id2"] = _row(2, payload="changed")  # 필드 불일치
    cmp = compare(src, tgt, committed={"id1", "id4", "ghost"})
    assert not cmp.passed
    assert cmp.missing_in_target == ["id4"]
    assert cmp.unexpected_in_target == ["extra"]
    assert cmp.field_mismatch[0]["request_id"] == "id2"
    assert cmp.field_mismatch[0]["fields"] == {"payload": {"source": "p2", "target": "changed"}}
    assert cmp.committed_missing_in_target == ["ghost", "id4"]
    report = cmp.as_report()
    assert report["counts"] == {"B_minus_T": 1, "T_minus_B": 1, "field_mismatch": 1, "S_minus_T": 2}
    assert report["samples"]["B_minus_T"] == ["id4"]


def test_compare_post_mode_allows_committed_extra_only():
    src = {"id1": _row(1)}
    tgt = {"id1": _row(1), "new1": _row(2, "aws_live"), "rogue": _row(3, "aws_live")}
    cmp = compare(src, tgt, committed={"id1", "new1"}, allowed_extra={"new1"})
    assert cmp.allowed_extra_in_target == ["new1"]
    assert cmp.unexpected_in_target == ["rogue"]
    assert not cmp.passed


def test_count_match_alone_does_not_pass():
    src = {"a": _row(1), "b": _row(2)}
    tgt = {"a": _row(1), "c": _row(2)}
    cmp = compare(src, tgt, committed=set())
    assert cmp.source_count == cmp.target_count and not cmp.passed


def test_post_mode_tolerates_other_writers_but_pre_mode_does_not():
    """전환 뒤 데모 앱·봇이 대상에만 쓴 행은 post 에서 실패가 아니다 (2026-11-05 리허설 T_minus_B=65 사례)."""
    from migration.verify import compare
    row = ("app", "rehearsal_live", 1, "p", "2026-11-05T00:00:00+00:00")
    source = {"s1": row}
    target = {"s1": row, "app1": row, "w1": row}
    pre = compare(source, target, {"s1"}, allowed_extra=set())
    assert pre.counts["T_minus_B"] == 2 and not pre.passed
    post = compare(source, target, {"s1", "w1"}, allowed_extra={"w1"}, tolerate_other_writers=True)
    assert post.passed and post.counts["T_minus_B"] == 0
    assert post.other_writer_in_target == ["app1"] and post.allowed_extra_in_target == ["w1"]
    assert post.as_report()["other_writer_in_target_count"] == 1


# ---- 기준점(watermark) 범위·digest 방식 ------------------------------------------


def test_parse_and_format_ts_round_trip():
    from migration.verify import format_ts, parse_ts
    assert parse_ts(None) is None and format_ts(None) is None
    ts = parse_ts("2026-11-08T00:00:00+09:00")
    assert format_ts(ts) == "2026-11-07T15:00:00.000000+00:00"
    assert parse_ts(datetime(2026, 1, 1)) == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_compare_digests_reports_only_different_buckets():
    from migration.verify import compare_digests
    src = {"0a": (10, "h1"), "0b": (5, "h2"), "0c": (1, "h3")}
    tgt = {"0a": (10, "h1"), "0b": (5, "xx"), "0d": (1, "h4")}
    assert compare_digests(src, tgt) == ["0b", "0c", "0d"]
    assert compare_digests(src, dict(src)) == []


def test_scope_clause_builds_half_open_range():
    from migration import db as dbmod
    from psycopg import sql
    since, upto = datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert dbmod.scope_clause(None, None) == (sql.SQL(""), ())
    where, params = dbmod.scope_clause(since, None)
    assert params == (since,) and "created_at > %s" in where.as_string(None)
    where, params = dbmod.scope_clause(since, upto)
    assert params == (since, upto) and "created_at <= %s" in where.as_string(None)


def test_committed_ids_in_scope_filters_by_created_at(tmp_path):
    from migration.state import RequestRecord, RequestStore
    store = RequestStore(tmp_path)
    try:
        for n, created in enumerate(("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01.500000+00:00",
                                     "2026-01-01T00:00:02+00:00")):
            rec = RequestRecord(request_id=f"id{n}", run_id="r", phase="p", request_no=n, payload="x", created_at=created)
            store.record_pending(rec)
            store.mark_committed(rec.request_id, "onprem" if n < 2 else "aws", 1)
        wm = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
        assert store.committed_ids_in_scope() == {"id0", "id1", "id2"}
        assert store.committed_ids_in_scope(since=wm) == {"id1", "id2"}          # 기준점 이후만
        assert store.committed_ids_in_scope(upto=wm) == {"id0"}                   # 기준점까지(포함)
        assert store.committed_ids_in_scope("aws", since=wm) == {"id2"}
    finally:
        store.close()


def test_digest_mode_rejects_post(tmp_path, monkeypatch):
    """post 모드에서는 대상에 새 행이 있어 digest 가 어긋나므로 설정 오류로 막는다."""
    import pytest
    from migration.config import ConfigError, parse_config
    from migration.verify import run
    from tests.conftest import RAW_CONFIG
    config = parse_config(RAW_CONFIG)
    with pytest.raises(ConfigError):
        run(config, source="onprem", target="aws", state_dir=None, mode="post", report_path=None, method="digest")
    with pytest.raises(ConfigError):
        run(config, source="onprem", target="aws", state_dir=None, mode="pre", report_path=None, method="bogus")
