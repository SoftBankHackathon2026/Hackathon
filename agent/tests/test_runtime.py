from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import __main__ as cli
from agent import runs as R
from agent.config import parse_config
from agent.notify import CompositeNotifier, SlackWebhookNotifier, StdoutNotifier
from agent.platform import handle_commands
from agent.runs import RunStore
from agent.runtime import AgentRuntime, SettingsError, validate

HOOK = "https://hooks.slack.com/services/T/B/SECRETPART"
RAW = {
    "job_id": "j", "policy": {"opa_url": "http://opa"},
    "plan": {"target_db": {"host": "db.svc", "port": 5432, "name": "app"}},
    "declared_evidence": {"target_db_exposure": {"public": False}, "target_location": {"location": "l"}},
    "smoke": {"target_only": True, "target_url": "http://t"},
    "openbao": {"addr": "http://bao", "mount": "secret", "path": "p"},
    "codex": {"runner": "opensandbox", "model": "gpt-5.5", "sandbox_url": "http://127.0.0.1:8090"},
    "slack": {"thread_ts": "1700000000.000100"},
}


def make(tmp_path: Path, env: dict | None = None) -> tuple[AgentRuntime, SimpleNamespace]:
    cfg = parse_config(RAW, {"VAULT_TOKEN": "t"})
    remediator = SimpleNamespace(auto_apply=False, d=SimpleNamespace(notifier=None))
    runtime = AgentRuntime(cfg, remediator, tmp_path, interval=60, env={"SLACK_WEBHOOK_URL": HOOK} if env is None else env)
    return runtime, remediator


@pytest.mark.parametrize("changes", [
    {}, {"evil": 1}, {"auto_apply": "yes"}, {"interval_seconds": 5}, {"interval_seconds": True},
    {"slack_thread_ts": "p1700000000000100"}, {"slack_enabled": 1},
])
def test_validate_rejects_bad_changes(changes: dict) -> None:
    with pytest.raises(SettingsError):
        validate(changes)


def test_apply_changes_runtime_and_notifier(tmp_path: Path) -> None:
    runtime, remediator = make(tmp_path)
    assert isinstance(remediator.d.notifier, CompositeNotifier)
    assert remediator.d.notifier.notifiers[1].thread_ts == "1700000000.000100"
    runtime.apply({"auto_apply": True, "interval_seconds": 120, "slack_thread_ts": ""})
    assert remediator.auto_apply is True and runtime.interval == 120
    hook = remediator.d.notifier.notifiers[1]
    assert isinstance(hook, SlackWebhookNotifier) and hook.thread_ts is None
    runtime.apply({"slack_enabled": False})
    assert isinstance(remediator.d.notifier, StdoutNotifier)


def test_settings_survive_restart(tmp_path: Path) -> None:
    runtime, _ = make(tmp_path)
    runtime.apply({"auto_apply": True, "interval_seconds": 300, "slack_enabled": False})
    again, remediator = make(tmp_path)
    assert remediator.auto_apply is True and again.interval == 300 and again.slack_enabled is False
    assert again.applied_at == runtime.applied_at


def test_broken_settings_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"interval_seconds": 1}))
    runtime, remediator = make(tmp_path)
    assert runtime.interval == 60 and remediator.auto_apply is False


def test_snapshot_shows_config_without_secrets(tmp_path: Path) -> None:
    runtime, _ = make(tmp_path)
    snap = runtime.snapshot()
    assert snap["slack"] == {"mode": "webhook", "enabled": True, "channel": None, "thread_ts": "1700000000.000100"}
    assert snap["fix"] == {"keys": ["DB_HOST", "DB_NAME", "DB_PORT"], "plan": {"DB_HOST": "db.svc", "DB_PORT": "5432", "DB_NAME": "app"}}
    assert snap["sandbox"] == {"runner": "opensandbox", "image": "oneport/codex-sandbox:latest", "model": "gpt-5.5"}
    assert "SECRETPART" not in json.dumps(snap)
    assert make(tmp_path / "x", env={})[0].snapshot()["slack"]["mode"] == "none"


class FakeClient:
    def __init__(self, batches: list[list[dict]]) -> None:
        self.batches, self.acks, self.beats = batches, [], []

    def commands(self):
        return self.batches.pop(0) if self.batches else []

    def ack_command(self, command_id, result):
        self.acks.append((command_id, result))

    def heartbeat(self, **kwargs):
        self.beats.append(kwargs)

    def approvals(self):
        return []

    def sync(self, runs, limit=20):
        return 0


def test_handle_commands_applies_rejects_and_reruns(tmp_path: Path) -> None:
    runtime, remediator = make(tmp_path)
    client = FakeClient([[
        {"id": 1, "kind": "settings", "payload": {"auto_apply": True}, "requested_by": "운영 화면"},
        {"id": 2, "kind": "settings", "payload": {"interval_seconds": 1}},
        {"id": 3, "kind": "rerun", "payload": {}},
    ]])
    assert handle_commands(client, runtime) is True
    assert remediator.auto_apply is True
    assert client.acks[0] == (1, "반영함: auto_apply=True")
    assert client.acks[1][0] == 2 and client.acks[1][1].startswith("거절함")
    assert client.acks[2] == (3, "바로 다시 판정함")


def test_watch_sends_config_and_rerun_cuts_wait_short(tmp_path: Path) -> None:
    runtime, remediator = make(tmp_path)
    runs = RunStore(tmp_path)
    calls = {"n": 0}

    def run():
        calls["n"] += 1
        r = runs.create("job")
        runs.update(r, R.NOTHING_TO_FIX, "정상")
        return r

    remediator.run = run
    client = FakeClient([[], [], [{"id": 9, "kind": "rerun", "payload": {}}]])
    sleeps: list[float] = []
    cli.watch(remediator, runs, interval=60, iterations=2, sleep=sleeps.append, platform=client, runtime=runtime)
    assert calls["n"] == 2
    assert sleeps == [10.0, 10.0]
    assert client.beats[0]["config"]["slack"]["mode"] == "webhook"
    assert client.beats[0]["interval_seconds"] == 60
    runs.close()


def test_settings_change_mid_wait_beats_now_and_uses_new_interval(tmp_path: Path) -> None:
    runtime, remediator = make(tmp_path)
    runs = RunStore(tmp_path)

    def run():
        r = runs.create("job")
        runs.update(r, R.NOTHING_TO_FIX, "정상")
        return r

    remediator.run = run
    client = FakeClient([[], [{"id": 5, "kind": "settings", "payload": {"interval_seconds": 30}}]])
    sleeps: list[float] = []
    cli.watch(remediator, runs, interval=60, iterations=2, sleep=sleeps.append, platform=client, runtime=runtime)
    assert sleeps == [10.0, 10.0, 10.0]
    assert [b["interval_seconds"] for b in client.beats] == [60, 30, 30]
    assert client.beats[1]["config"]["settings_applied_at"] is not None
    runs.close()
