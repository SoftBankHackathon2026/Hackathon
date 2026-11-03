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
