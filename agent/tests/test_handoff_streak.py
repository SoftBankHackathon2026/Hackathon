from __future__ import annotations

from agent import runs as R
from agent.fakes import FakeEvaluator
from agent.platform import PlatformClient

from test_notify_format import good_kv
from test_platform import FakeOpener


def hold(w, reason: str = "EVIDENCE-MISSING") -> None:
    w.evaluator.extra_reason, w.evaluator.force_outcome = reason, "hold"


def healthy(w) -> None:
    w.evaluator.extra_reason, w.evaluator.force_outcome = None, None


def test_repeats_fold_into_one_task_and_recovery_closes_it(make_world) -> None:
    w = make_world(kv=good_kv(), evaluator=FakeEvaluator(extra_reason="EVIDENCE-MISSING", force_outcome="hold"))
    first = w.remediator.run()
    for _ in range(3):
        w.remediator.run()
    origin = w.runs.get(first.run_id)
    assert origin.state == R.HANDED_OFF and origin.detail["repeats"] == 4
    assert [e["message"] for e in w.runs.events(first.run_id)][-1] == "같은 사유로 다시 넘김 (4번째)"
    healthy(w)
    assert w.remediator.run().state == R.NOTHING_TO_FIX
    closed = w.runs.get(first.run_id)
    assert closed.detail["closed_at"] and closed.state == R.HANDED_OFF
    assert "같은 사유로 4번 넘겼고" in w.runs.events(first.run_id)[-1]["message"]
    assert [n.split("  ")[0] for n in w.notes] == ["🙋 *사람 확인 필요*", "🟢 *다시 정상*"]
    assert f"`{first.run_id}`" in w.notes[-1]


def test_after_recovery_the_same_reason_opens_a_new_task(make_world) -> None:
    w = make_world(kv=good_kv(), evaluator=FakeEvaluator(extra_reason="EVIDENCE-MISSING", force_outcome="hold"))
    first = w.remediator.run()
    healthy(w)
    w.remediator.run()
    hold(w)
    second = w.remediator.run()
    assert second.state == R.HANDED_OFF and second.run_id != first.run_id
    assert w.remediator.run().state == R.REPEATED
    assert w.runs.get(second.run_id).detail["repeats"] == 2


def test_a_different_reason_closes_the_old_task_and_starts_another(make_world) -> None:
    w = make_world(kv=good_kv(), evaluator=FakeEvaluator(extra_reason="EVIDENCE-MISSING", force_outcome="hold"))
    first = w.remediator.run()
    hold(w, "OTHER-REASON")
    other = w.remediator.run()
    assert other.state == R.HANDED_OFF
    assert w.runs.get(first.run_id).detail["closed_by"] == other.run_id


def test_quiet_unverified_streak_recovers_without_a_message(make_world) -> None:
    w = make_world(kv=good_kv(), evaluator=FakeEvaluator(extra_reason="APP-01-UNVERIFIED", force_outcome="hold"))
    first = w.remediator.run()
    w.remediator.run()
    healthy(w)
    w.remediator.run()
    assert w.notes == []
    assert w.runs.get(first.run_id).detail["closed_at"]


def test_a_fix_proposal_closes_the_open_task(make_world) -> None:
    w = make_world(auto_apply=False, evaluator=FakeEvaluator(extra_reason="EVIDENCE-MISSING", force_outcome="hold"))
    first = w.remediator.run()
    healthy(w)
    run = w.remediator.run()
    assert run.state == R.WAITING_APPROVAL
    assert w.runs.get(first.run_id).detail["closed_by"] == run.run_id


def test_platform_sync_skips_folded_repeats(make_world) -> None:
    w = make_world(kv=good_kv(), evaluator=FakeEvaluator(extra_reason="EVIDENCE-MISSING", force_outcome="hold"))
    first = w.remediator.run()
    w.remediator.run()
    w.remediator.run()
    opener = FakeOpener()
    client = PlatformClient("https://platform.example", "t", "a", opener=opener)
    assert client.sync(w.runs) == 1
    assert opener.requests[0][1] == f"/api/v1/ingest/agent/runs/{first.run_id}"
