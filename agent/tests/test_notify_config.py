from __future__ import annotations

import json

import pytest

from agent.config import ConfigError, parse_config
from agent.notify import CompositeNotifier, StdoutNotifier, build_notifier
from agent.probe import password_from_url

RAW = {
    "job_id": "j", "policy": {"opa_url": "http://opa"},
    "plan": {"target_db": {"host": "h", "port": 5432}, "limits": {"evidence_ttl_seconds": 300}},
    "declared_evidence": {"target_db_exposure": {"public": False}, "target_location": {"location": "l"}},
    "smoke": {"source_url": "http://s", "target_url": "http://t"},
    "openbao": {"addr": "http://bao", "mount": "secret", "path": "p"},
}


def test_store_location_from_secret_env_wins() -> None:
    cfg = parse_config(RAW, {"VAULT_TOKEN": "tok", "VAULT_ADDR": "http://other", "VAULT_PATH": "other/path"})
    assert cfg.openbao.addr == "http://other" and cfg.openbao.path == "other/path" and cfg.openbao.mount == "secret"


def test_token_required_and_hidden_from_repr() -> None:
    with pytest.raises(ConfigError):
        parse_config(RAW, {})
    assert "tok-value" not in repr(parse_config(RAW, {"VAULT_TOKEN": "tok-value"}))


def test_plan_target_db_required() -> None:
    bad = {**RAW, "plan": {"limits": {}}}
    with pytest.raises(ConfigError):
        parse_config(bad, {"VAULT_TOKEN": "t"})


def test_slack_only_when_token_and_channel() -> None:
    assert isinstance(build_notifier(None, {}), StdoutNotifier)
    assert isinstance(build_notifier("C1", {}), StdoutNotifier)
    assert isinstance(build_notifier(None, {"SLACK_BOT_TOKEN": "x"}), StdoutNotifier)
    assert isinstance(build_notifier("C1", {"SLACK_BOT_TOKEN": "x"}), CompositeNotifier)


def test_password_from_url_decodes() -> None:
    assert password_from_url("postgresql://u:p%40ss%2Fw@h:5432/d") == "p@ss/w"
    assert password_from_url("postgresql://u@h/d") is None
    assert password_from_url(None) is None


def test_target_only_does_not_need_source_url() -> None:
    raw = {**RAW, "smoke": {"target_only": True, "target_url": "http://t"}}
    cfg = parse_config(raw, {"VAULT_TOKEN": "t"})
    assert cfg.smoke.target_only is True and cfg.smoke.source_url is None
    with pytest.raises(ConfigError, match="source_url"):
        parse_config({**RAW, "smoke": {"target_url": "http://t"}}, {"VAULT_TOKEN": "t"})


def test_webhook_wins_and_posts_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import notify

    url = "https://hooks.slack.com/services/T000/B000/XXXX"
    notifier = build_notifier(None, {"SLACK_WEBHOOK_URL": url, "SLACK_BOT_TOKEN": "x"})
    assert isinstance(notifier, CompositeNotifier)
    hook = notifier.notifiers[1]
    assert isinstance(hook, notify.SlackWebhookNotifier) and "XXXX" not in repr(hook)
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            return b"ok"

    def open_(req, timeout):
        seen["url"], seen["body"] = req.full_url, req.data
        return Resp()

    monkeypatch.setattr(hook._opener, "open", open_)
    hook.send("[agent run-1] 복구 완료")
    assert seen["url"] == url
    assert json.loads(seen["body"]) == {"text": "[agent run-1] 복구 완료"}


def test_non_slack_webhook_is_ignored() -> None:
    assert isinstance(build_notifier(None, {"SLACK_WEBHOOK_URL": "https://evil.example/hook"}), StdoutNotifier)


def test_webhook_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import notify

    hook = notify.SlackWebhookNotifier("https://hooks.slack.com/services/T/B/X")

    def boom(req, timeout):
        raise OSError("down")

    monkeypatch.setattr(hook._opener, "open", boom)
    hook.send("text")
