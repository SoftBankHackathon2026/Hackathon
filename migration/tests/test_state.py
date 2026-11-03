"""state.py: 요청 기록·재시작 복구·전환 체크포인트."""

from __future__ import annotations

from migration.state import COMMITTED, PENDING, QUEUE_REJECTED, CutoverJob, CutoverStore, RequestRecord, RequestStore


def _rec(n: int, rid: str | None = None) -> RequestRecord:
    return RequestRecord(request_id=rid or f"id-{n}", run_id="r", phase="onprem_live", request_no=n,
                         payload=f"p{n}", created_at=f"2026-01-01T00:00:{n:02d}+00:00")


def test_pending_then_committed_roundtrip(state_dir):
    store = RequestStore(state_dir)
    assert store.record_pending(_rec(1)) is True
    assert store.record_pending(_rec(1)) is False  # 같은 ID 는 다시 만들지 않는다
    assert [r.request_id for r in store.pending()] == ["id-1"]
    store.mark_committed("id-1", "onprem", attempts=2)
    assert store.pending() == []
    got = store.get("id-1")
    assert got is not None and got.status == COMMITTED and got.target == "onprem" and got.attempts == 2
    assert store.committed_ids() == {"id-1"}
    assert store.committed_ids("aws") == set()
    assert store.counts()[COMMITTED] == 1
    store.close()


def test_restart_sees_pending_in_order(state_dir):
    store = RequestStore(state_dir)
    for n in (3, 1, 2):
        store.record_pending(_rec(n))
    store.record_attempt("id-2", "onprem", 1, "boom")
    store.close()
    store2 = RequestStore(state_dir)
    assert [r.request_id for r in store2.pending()] == ["id-1", "id-2", "id-3"]
    assert store2.get("id-2").attempts == 1 and store2.get("id-2").last_error == "boom"
    assert store2.max_request_no("r") == 3
    store2.close()


def test_rejected_and_mismatch_are_recorded(state_dir):
    store = RequestStore(state_dir)
    store.record_rejected(_rec(9), "queue_full")
    assert store.get("id-9").status == QUEUE_REJECTED
    store.record_pending(_rec(10))
    store.mark_mismatch("id-10", "onprem", 1, "diff")
    assert store.counts()["mismatch"] == 1 and store.counts()[PENDING] == 0
    store.close()


def test_last_committed_by_target(state_dir):
    store = RequestStore(state_dir)
    store.record_pending(_rec(1)); store.mark_committed("id-1", "onprem", 1, "2026-01-01T00:00:01+00:00")
    store.record_pending(_rec(2)); store.mark_committed("id-2", "aws", 1, "2026-01-01T00:00:02+00:00")
    assert store.last_committed("onprem").request_id == "id-1"
    assert store.last_committed().request_id == "id-2"
    assert store.counts_by_target() == {"onprem": 1, "aws": 1}
    store.close()


def test_cutover_store_roundtrip(state_dir):
    store = CutoverStore(state_dir)
    assert store.load("forward_001") is None
    job = CutoverJob("forward_001", "onprem->aws", "onprem", "aws", "COPYING", {"a": 1}, "")
    store.save(job)
    store.save(CutoverJob("forward_001", "onprem->aws", "onprem", "aws", "READY", {"a": 2, "b": [1]}, ""))
    loaded = store.load("forward_001")
    assert loaded.stage == "READY" and loaded.checkpoint == {"a": 2, "b": [1]}
    assert store.step_done("forward_001", "x") is None
    store.mark_step("forward_001", "x", {"result": 5})
    assert store.step_done("forward_001", "x") == {"result": 5}
    assert [s for s, _ in store.steps("forward_001")] == ["x"]
    store.close()
