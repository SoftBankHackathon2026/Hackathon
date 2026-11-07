import importlib.util
import json
import shlex
import sqlite3
import sys
import urllib.error
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


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://platform.example/api", code, "Bad Gateway", {}, None)


def test_main_one_shot_returns_1_but_does_not_crash_on_platform_502(monkeypatch, capsys):
    monkeypatch.setenv("ONEPORT_INGEST_TOKEN", "token")

    def failing_report(args, token):
        raise _http_error(502)

    monkeypatch.setattr(reporter, "report_once", failing_report)
    rc = reporter.main(["--job-id", "forward_001", "--platform-url", "https://platform.example"])
    assert rc == 1
    assert "report failed" in capsys.readouterr().out


def test_main_loop_keeps_running_after_platform_502(monkeypatch):
    monkeypatch.setenv("ONEPORT_INGEST_TOKEN", "token")
    calls: list[str] = []

    def flaky_report(args, token):
        calls.append("report")
        if len(calls) == 1:
            raise _http_error(502)

    def stop_after_sleep(seconds):
        calls.append("sleep")
        if calls.count("sleep") == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(reporter, "report_once", flaky_report)
    monkeypatch.setattr(reporter, "claim_command", lambda args, token: None)
    monkeypatch.setattr(reporter.time, "sleep", stop_after_sleep)
    with pytest.raises(KeyboardInterrupt):
        reporter.main(
            [
                "--job-id",
                "forward_001",
                "--platform-url",
                "https://platform.example",
                "--interval",
                "5",
                "--execute-commands",
            ]
        )
    # 첫 보고가 502 로 실패해도 다음 주기 보고까지 이어진다.
    assert calls == ["report", "sleep", "report", "sleep"]


BASE_CONFIG = """
targets:
  onprem: {host: postgres}
  onprem_return: {host: postgres}
  aws: {host: rds}
job:
  job_id: durable_forward_001
  source: onprem_return
  target: aws
  table: public.migration_demo
kafka: {bootstrap_servers: kafka:9092}
"""

CLAIMED = {
    "command": {"id": "cmd_9", "action": "run", "approval_id": None},
    "job_id": "onprem_to_aws_001",
    "source_env": "onprem",
    "target_env": "aws",
    "data_source": "onprem_return",
    "data_target": "aws",
}


def test_report_all_reports_every_job_in_the_state_and_skips_unregistered(tmp_path, monkeypatch):
    make_state(tmp_path)
    with sqlite3.connect(tmp_path / "cutover.sqlite3") as conn:
        conn.execute(
            "INSERT INTO cutover_jobs VALUES ('old_001','forward','onprem','aws','COMPLETED','{}','2026-11-05T01:00:00Z')"
        )
    sent = []

    def post(url, token, body):
        if url.endswith("/old_001"):
            raise _http_error(404)
        sent.append(url)
        return 202

    monkeypatch.setattr(reporter, "post", post)
    args = Namespace(state_dir=str(tmp_path), report_dir=None, platform_url="http://p")
    reporter.report_all(args, "token")
    assert sent[0] == "http://p/api/v1/ingest/migrations/forward_001"
    assert all("old_001" not in url for url in sent)


def test_report_all_still_raises_other_platform_errors(tmp_path, monkeypatch):
    make_state(tmp_path)

    def post(url, token, body):
        raise _http_error(502)

    monkeypatch.setattr(reporter, "post", post)
    args = Namespace(state_dir=str(tmp_path), report_dir=None, platform_url="http://p")
    with pytest.raises(urllib.error.HTTPError):
        reporter.report_all(args, "token")


def test_job_config_swaps_only_the_job_section(tmp_path):
    import yaml

    base = tmp_path / "config.yaml"
    base.write_text(BASE_CONFIG)
    path = reporter.job_config(str(base), CLAIMED, tmp_path / "jobs")
    config = yaml.safe_load(path.read_text())
    assert path == tmp_path / "jobs" / "onprem_to_aws_001.yaml"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert config["job"] == {
        "job_id": "onprem_to_aws_001",
        "source": "onprem_return",
        "target": "aws",
        "table": "public.migration_demo",
        "live_phase_before": "onprem_return_live",
        "live_phase_after": "aws_live",
    }
    assert config["targets"]["aws"] == {"host": "rds"}
    assert config["kafka"] == {"bootstrap_servers": "kafka:9092"}


def test_job_config_rejects_targets_the_runner_does_not_know(tmp_path):
    base = tmp_path / "config.yaml"
    base.write_text(BASE_CONFIG)
    unknown = {**CLAIMED, "data_target": "connected-7"}
    with pytest.raises(ValueError, match="connected-7"):
        reporter.job_config(str(base), unknown, tmp_path / "jobs")


def test_execute_any_runs_the_claimed_task_with_its_own_config(tmp_path, monkeypatch):
    base = tmp_path / "config.yaml"
    base.write_text(BASE_CONFIG)
    script = "import sys; print(' '.join(sys.argv[1:])); sys.exit(3)"
    monkeypatch.setenv("ONEPORT_CUTOVER_ARGV", f"{sys.executable} -c {shlex.quote(script)}")
    monkeypatch.setenv("MIGRATION_CONFIG", str(base))
    posted, reported = [], []
    monkeypatch.setattr(reporter, "claim_any_command", lambda args, token: CLAIMED)
    monkeypatch.setattr(
        reporter, "post_json", lambda url, token, body: posted.append((url, body)) or (200, body)
    )
    monkeypatch.setattr(reporter, "report_job", lambda args, token, job: reported.append(job))
    args = Namespace(state_dir=str(tmp_path), platform_url="http://p/", interval=5)
    assert reporter.execute_any(args, "token") is True
    url, body = posted[-1]
    assert url == "http://p/api/v1/ingest/migrations/onprem_to_aws_001/commands/cmd_9/result"
    assert body["exit_code"] == 3
    assert str(tmp_path / "jobs" / "onprem_to_aws_001.yaml") in body["log_tail"]
    assert reported[-1] == "onprem_to_aws_001"


def test_execute_any_reports_a_config_error_for_unknown_targets(tmp_path, monkeypatch):
    base = tmp_path / "config.yaml"
    base.write_text(BASE_CONFIG)
    monkeypatch.setenv("MIGRATION_CONFIG", str(base))
    posted = []
    monkeypatch.setattr(
        reporter, "claim_any_command", lambda args, token: {**CLAIMED, "data_source": "nope"}
    )
    monkeypatch.setattr(
        reporter, "post_json", lambda url, token, body: posted.append(body) or (200, body)
    )
    monkeypatch.setattr(reporter, "report_job", lambda args, token, job: None)
    args = Namespace(state_dir=str(tmp_path), platform_url="http://p", interval=5)
    assert reporter.execute_any(args, "token") is True
    assert posted[-1]["exit_code"] == reporter.EXIT_CONFIG and "nope" in posted[-1]["log_tail"]


def test_execute_any_returns_false_when_nothing_is_pending(monkeypatch):
    monkeypatch.setattr(reporter, "post_json", lambda url, token, body: (204, None))
    args = Namespace(state_dir="/nope", platform_url="http://p")
    assert reporter.execute_any(args, "token") is False


def test_main_all_jobs_reports_every_job_and_runs_any_command(monkeypatch):
    monkeypatch.setenv("ONEPORT_INGEST_TOKEN", "token")
    calls = []
    monkeypatch.setattr(reporter, "report_all", lambda args, token: calls.append("report_all"))
    monkeypatch.setattr(reporter, "execute_any", lambda args, token: calls.append("execute_any"))
    monkeypatch.setattr(reporter, "report_once", lambda args, token: calls.append("report_once"))
    rc = reporter.main(["--all-jobs", "--platform-url", "http://p", "--execute-commands"])
    assert rc == 0 and calls == ["report_all", "execute_any"]


def test_main_needs_exactly_one_of_job_id_or_all_jobs(monkeypatch):
    monkeypatch.setenv("ONEPORT_INGEST_TOKEN", "token")
    with pytest.raises(SystemExit):
        reporter.main(["--platform-url", "http://p"])
    with pytest.raises(SystemExit):
        reporter.main(["--job-id", "a", "--all-jobs", "--platform-url", "http://p"])
