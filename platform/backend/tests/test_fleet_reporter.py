import importlib.util
import json
import shlex
import sqlite3
import sys
from argparse import Namespace
from pathlib import Path

import pytest

path = Path(__file__).parents[2] / "deploy" / "onprem" / "fleet_reporter.py"
spec = importlib.util.spec_from_file_location("fleet_reporter", path)
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


def make_state(state_dir: Path) -> None:
    with sqlite3.connect(state_dir / "cutover.sqlite3") as conn:
        conn.execute(
            "CREATE TABLE cutover_jobs (job_id TEXT, direction TEXT, source TEXT, target TEXT, stage TEXT, checkpoint TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO cutover_jobs VALUES ('forward_001','forward','onprem','aws','READY','{\"precheck\": {}}','2026-11-05T01:00:00Z')"
        )
    with sqlite3.connect(state_dir / "policy.sqlite3") as conn:
        conn.execute(
            "CREATE TABLE policy_decisions (decision_id TEXT, job_id TEXT, stage TEXT, outcome TEXT, reason_codes TEXT, policy_version TEXT, evaluated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO policy_decisions VALUES ('d1','forward_001','pre_start','allow','[]','migration-policy-v1','2026-11-05T00:59:00Z')"
        )
    reports = state_dir / "reports"
    reports.mkdir()
    for label in ("pre-stop", "post-stop"):
        (reports / f"verify-forward_001-{label}.json").write_text(json.dumps({"label": label}))


def test_reporter_pushes_cutover_decisions_and_latest_verify(tmp_path, monkeypatch):
    make_state(tmp_path)
    sent = []
    monkeypatch.setattr(reporter, "post", lambda url, token, body: sent.append((url, body)) or 202)
    args = Namespace(
        job_id="forward_001",
        state_dir=str(tmp_path),
        report_dir=None,
        platform_url="http://platform/",
    )
    reporter.report_once(args, "token")
    assert sent[0][0] == "http://platform/api/v1/ingest/migrations/forward_001"
    assert sent[0][1]["cutover"]["stage"] == "READY"
    assert sent[0][1]["cutover"]["checkpoint"] == {"precheck": {}}
    assert sent[0][1]["policy_decisions"][0]["reason_codes"] == []
    assert sent[1][1]["verify"] == {"mode": "pre", "report": {"label": "post-stop"}}
    assert len(sent) == 2


def test_reporter_tolerates_missing_state(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(reporter, "post", lambda url, token, body: sent.append(body) or 202)
    args = Namespace(
        job_id="return_001", state_dir=str(tmp_path), report_dir=None, platform_url="http://p"
    )
    reporter.report_once(args, "token")
    assert sent == [{"cutover": None, "policy_decisions": []}]


def test_command_argv_maps_every_action_and_requires_approval_id():
    base = ["python", "-m", "migration.cutover"]
    prefix = base + ["--config", "/etc/live-cutover/config.yaml", "--state-dir", "/var/lib/worker"]
    cfg = "/etc/live-cutover/config.yaml"
    assert reporter.command_argv({"action": "run"}, base, cfg, "/var/lib/worker") == prefix + [
        "run"
    ]
    assert reporter.command_argv(
        {"action": "approve", "approval_id": "approve-forward-001"}, base, cfg, "/var/lib/worker"
    ) == prefix + ["run", "--approve", "approve-forward-001"]
    assert reporter.command_argv({"action": "retry"}, base, cfg, "/var/lib/worker")[-2:] == [
        "run",
        "--retry",
    ]
    assert reporter.command_argv({"action": "force_retry"}, base, cfg, "/var/lib/worker")[-2:] == [
        "run",
        "--force-retry",
    ]
    assert reporter.command_argv({"action": "abort"}, base, None, "/s") == base + [
        "--state-dir",
        "/s",
        "abort",
    ]
    with pytest.raises(ValueError):
        reporter.command_argv({"action": "approve", "approval_id": " "}, base, cfg, "/s")
    with pytest.raises(ValueError):
        reporter.command_argv({"action": "rm_rf"}, base, cfg, "/s")


def test_redact_hides_passwords_and_tokens_but_keeps_the_rest():
    text = 'MIGRATION_PASSWORD=hunter2 token: abc.def password="p w" done'
    redacted = reporter.redact(text)
    assert "hunter2" not in redacted and "abc.def" not in redacted
    assert redacted.endswith('w" done') or "done" in redacted
    assert redacted.startswith("MIGRATION_PASSWORD=[redacted]")


def test_execute_once_runs_the_claimed_command_and_posts_redacted_result(tmp_path, monkeypatch):
    make_state(tmp_path)
    script = "import sys; print('PGPASSWORD=secret123 approved'); print('x' * 6000); sys.exit(3)"
    monkeypatch.setenv("ONEPORT_CUTOVER_ARGV", f"{sys.executable} -c {shlex.quote(script)}")
    monkeypatch.setenv("MIGRATION_CONFIG", "/etc/live-cutover/config.yaml")
    posted = []
    monkeypatch.setattr(
        reporter,
        "claim_command",
        lambda args, token: {
            "id": "cmd_1",
            "action": "approve",
            "approval_id": "approve-forward-001",
        },
    )
    monkeypatch.setattr(
        reporter, "post_json", lambda url, token, body: posted.append((url, body)) or (200, body)
    )
    args = Namespace(job_id="forward_001", state_dir=str(tmp_path), platform_url="http://p/")
    assert reporter.execute_once(args, "token") is True
    url, body = posted[-1]
    assert url == "http://p/api/v1/ingest/migrations/forward_001/commands/cmd_1/result"
    assert body["exit_code"] == 3 and body["stage"] == "READY"
    assert len(body["log_tail"]) == 4000 and "secret123" not in body["log_tail"]


def test_execute_once_returns_false_when_nothing_is_pending(monkeypatch):
    monkeypatch.setattr(reporter, "post_json", lambda url, token, body: (204, None))
    args = Namespace(job_id="forward_001", state_dir="/nope", platform_url="http://p")
    assert reporter.execute_once(args, "token") is False


def test_run_command_keeps_reporting_while_subprocess_runs(tmp_path):
    import sys as _sys

    script = tmp_path / "slow.py"
    script.write_text(
        "import sys, time\n"
        "print('start password=abc123')\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.9)\n"
        "print('done')\n"
        "sys.exit(3)\n"
    )
    ticks: list[int] = []
    code, tail = reporter.run_command(
        [_sys.executable, "-I", str(script)], on_tick=lambda: ticks.append(1), tick_seconds=0.2
    )
    assert code == 3
    assert "done" in tail and "abc123" not in tail
    assert len(ticks) >= 2, ticks
