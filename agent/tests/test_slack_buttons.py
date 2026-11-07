from __future__ import annotations

import json

import pytest

from agent import runs as R
from agent.config import ConfigError, parse_config
from agent.notify import CompositeNotifier, SlackWebhookNotifier, StdoutNotifier

RAW = {
    "job_id": "j", "policy": {"opa_url": "http://opa"}, "plan": {"target_db": {"host": "h", "port": 5432}},
    "declared_evidence": {"target_db_exposure": {"public": False}, "target_location": {"location": "l"}},
    "smoke": {"target_only": True, "target_url": "http://t"},
    "openbao": {"addr": "http://bao", "mount": "secret", "path": "p"},
}


def test_buttons_flag_is_off_by_default_and_must_be_bool() -> None:
    assert parse_config(RAW, {"VAULT_TOKEN": "t"}).slack_buttons is False
    assert parse_config({**RAW, "slack": {"buttons": True}}, {"VAULT_TOKEN": "t"}).slack_buttons is True
    with pytest.raises(ConfigError):
        parse_config({**RAW, "slack": {"buttons": "yes"}}, {"VAULT_TOKEN": "t"})


def approval_blocks(w):
    return next(b for t, b in zip(w.notes, w.remediator.d.notifier.blocks) if t.startswith("🟡"))


def test_waiting_message_gets_buttons_and_platform_link(make_world) -> None:
    w = make_world(auto_apply=False)
    w.remediator.slack_buttons = True
    w.remediator.approve_url = "https://platform.example/"
    run = w.remediator.run()
    assert run.state == R.WAITING_APPROVAL
    blocks = approval_blocks(w)
    assert [b["type"] for b in blocks] == ["section", "actions", "context"]
    assert "*승인*" not in blocks[0]["text"]["text"] and "DB_HOST" in blocks[0]["text"]["text"]
    approve, reject, link = blocks[1]["elements"]
    assert (approve["action_id"], approve["value"], approve["style"]) == ("agent_approve", run.run_id, "primary")
    assert "DB_HOST 값을" in approve["confirm"]["text"]["text"]
    assert (reject["action_id"], reject["value"], reject["style"]) == ("agent_reject", run.run_id, "danger")
    assert link["url"] == f"https://platform.example/#agent/{run.run_id}"
    assert run.run_id in blocks[2]["elements"][0]["text"]
    waiting = next(t for t in w.notes if t.startswith("🟡"))
    assert "플랫폼에서 승인" in waiting


def test_no_buttons_unless_enabled_and_no_link_without_platform(make_world) -> None:
    w = make_world(auto_apply=False)
    w.remediator.run()
    assert approval_blocks(w) is None
    w2 = make_world(auto_apply=False)
    w2.remediator.slack_buttons = True
    w2.remediator.run()
    assert [e["action_id"] for e in approval_blocks(w2)[1]["elements"]] == ["agent_approve", "agent_reject"]


def test_webhook_sends_blocks_with_text_fallback(monkeypatch) -> None:
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return b"ok"

    hook = SlackWebhookNotifier("https://hooks.slack.com/services/T/B/X", thread_ts="1700000000.000100")
    monkeypatch.setattr(hook._opener, "open", lambda req, timeout: seen.update(body=json.loads(req.data)) or Resp())
    CompositeNotifier(StdoutNotifier(), hook).send("hello", [{"type": "section"}])
    assert seen["body"] == {"text": "hello", "blocks": [{"type": "section"}], "thread_ts": "1700000000.000100"}
    hook.send("plain")
    assert "blocks" not in seen["body"]
