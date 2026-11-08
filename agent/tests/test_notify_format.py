from __future__ import annotations

import json

import pytest

from agent import notify
from agent.config import ConfigError, parse_config
from agent.fakes import GOOD_HOST, FakeEvaluator, FakeKv

def good_kv() -> FakeKv:
    return FakeKv({"DB_HOST": GOOD_HOST, "DB_PORT": "5432", "DB_NAME": "migration_demo", "DB_USER": "migration_app"})


RAW = {
    "job_id": "j", "policy": {"opa_url": "http://opa"},
    "plan": {"target_db": {"host": "h", "port": 5432}},
    "declared_evidence": {"target_db_exposure": {"public": False}, "target_location": {"location": "l"}},
    "smoke": {"target_only": True, "target_url": "http://t"},
    "openbao": {"addr": "http://bao", "mount": "secret", "path": "p"},
}


def test_nothing_to_fix_is_not_announced(make_world) -> None:
    w = make_world(kv=good_kv())
    assert w.remediator.run().state == "NOTHING_TO_FIX"
    assert w.notes == []


def test_same_hand_off_is_announced_once(make_world) -> None:
    w = make_world(kv=good_kv(), evaluator=FakeEvaluator(extra_reason="EVIDENCE-MISSING", force_outcome="hold"))
    states = [w.remediator.run().state for _ in range(3)]
    assert states == ["HANDED_OFF", "REPEATED", "REPEATED"]
    assert len(w.notes) == 1 and w.notes[0].startswith("🙋 *사람 확인 필요*")


def test_waiting_message_shows_change_and_copyable_command(make_world) -> None:
    w = make_world(auto_apply=False)
    w.remediator.approve_command = "oneport-agent approve"
    run = w.remediator.run()
    assert run.state == "WAITING_APPROVAL"
    waiting = next(n for n in w.notes if n.startswith("🟡 *승인 대기*"))
    assert f"`{run.run_id}`" in waiting
    assert "• `DB_HOST`  `" in waiting and "` → `" in waiting
    assert f"*승인*  `oneport-agent approve {run.run_id}`" in waiting
    assert w.notes[0].startswith("🔎 *AI 수정 시작*")


def test_resolved_flow_titles(make_world) -> None:
    w = make_world()
    assert w.remediator.run().state == "RESOLVED"
    assert [n.split("  ")[0] for n in w.notes] == ["🔎 *AI 수정 시작*", "🛠 *저장소 반영*", "✅ *복구 완료*"]


def test_thread_ts_is_sent_in_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    hook = notify.SlackWebhookNotifier("https://hooks.slack.com/services/T/B/X", thread_ts="1700000000.000100")
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            return b"ok"

    def open_(req, timeout):
        seen["body"] = json.loads(req.data)
        return Resp()

    monkeypatch.setattr(hook._opener, "open", open_)
    hook.send("hello")
    assert seen["body"] == {"text": "hello", "thread_ts": "1700000000.000100"}


def test_config_reads_thread_and_approve_command() -> None:
    raw = {**RAW, "slack": {"thread_ts": "1700000000.000100"}, "approve_command": "oneport-agent approve"}
    cfg = parse_config(raw, {"VAULT_TOKEN": "t"})
    assert cfg.slack_thread_ts == "1700000000.000100" and cfg.approve_command == "oneport-agent approve"
    with pytest.raises(ConfigError):
        parse_config({**RAW, "slack": {"thread_ts": "p1700000000000100"}}, {"VAULT_TOKEN": "t"})


def test_unverified_hold_alerts_only_after_three_in_a_row(make_world) -> None:
    w = make_world(kv=good_kv(), evaluator=FakeEvaluator(extra_reason="APP-01-UNVERIFIED", force_outcome="hold"))
    first = w.remediator.run()
    assert first.state == "HANDED_OFF"
    assert w.remediator.run().state == "REPEATED"
    assert w.notes == []
    w.remediator.run()
    assert len(w.notes) == 1 and w.notes[0].startswith("🙋") and f"`{first.run_id}`" in w.notes[0]
    assert "같은 사유 3번째" in w.notes[0]
    for _ in range(3):
        w.remediator.run()
    assert len(w.notes) == 1


def test_unverified_blip_between_normal_runs_stays_quiet(make_world) -> None:
    from agent.remediation import Remediator

    w = make_world(kv=good_kv(), evaluator=FakeEvaluator(extra_reason="APP-01-UNVERIFIED", force_outcome="hold"))
    w.remediator.run()
    w.evaluator.extra_reason, w.evaluator.force_outcome = None, None
    assert w.remediator.run().state == "NOTHING_TO_FIX"
    w.evaluator.extra_reason, w.evaluator.force_outcome = "APP-01-UNVERIFIED", "hold"
    w.remediator.run()
    w.remediator.run()
    assert w.notes == []
