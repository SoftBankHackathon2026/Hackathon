import importlib.util
import json
import shlex
import socket
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


FULL_CONFIG = (
    BASE_CONFIG
    + """
full_migration: {destination_target: aws_eks}
platform_data_targets: {onprem: onprem_return, aws: aws}
platform_apps: [sample-app, hackathon/Deployment/sample-app, hackathon/Rollout/sample-app]
"""
)


@pytest.mark.parametrize("source", ["onprem", "onprem_return"])
@pytest.mark.parametrize(
    "app", ["sample-app", "hackathon/Deployment/sample-app", "hackathon/Rollout/sample-app"]
)
def test_full_job_config_resolves_only_explicit_platform_aliases(tmp_path, source, app):
    import yaml

    base = tmp_path / "config.yaml"
    base.write_text(FULL_CONFIG)
    claimed = {**CLAIMED, "data_source": source, "apps": [app]}
    path = reporter.job_config(str(base), claimed, tmp_path / "jobs")
    config = yaml.safe_load(path.read_text())
    assert (config["job"]["source"], config["job"]["target"]) == ("onprem_return", "aws")
    assert config["full_migration"] == {"destination_target": "aws_eks"}


@pytest.mark.parametrize(
    "override",
    [
        {"data_source": "aws"},
        {"data_target": "onprem_return"},
        {"source_env": "connected-7"},
        {
            "source_env": "aws",
            "target_env": "onprem",
            "data_source": "aws",
            "data_target": "onprem_return",
        },
        {"apps": []},
        {"apps": ["other-app"]},
        {"apps": ["sample-app", "other-app"]},
        {"apps": ["sample-app", "sample-app"]},
        {"apps": ["other-namespace/Deployment/sample-app"]},
    ],
)
def test_full_job_config_rejects_wrong_database_direction_or_apps(tmp_path, override):
    base = tmp_path / "config.yaml"
    base.write_text(FULL_CONFIG)
    claimed = {**CLAIMED, "apps": ["sample-app"], **override}
    with pytest.raises(ValueError, match="full migration"):
        reporter.job_config(str(base), claimed, tmp_path / "jobs")
    assert not (tmp_path / "jobs").exists()


@pytest.mark.parametrize("remove", ["platform_data_targets", "platform_apps"])
def test_full_job_config_requires_explicit_platform_contract(tmp_path, remove):
    import yaml

    config = yaml.safe_load(FULL_CONFIG)
    del config[remove]
    base = tmp_path / "config.yaml"
    base.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match="full migration"):
        reporter.job_config(str(base), {**CLAIMED, "apps": ["sample-app"]}, tmp_path / "jobs")


def test_db_only_job_config_preserves_platform_target_without_full_remapping(tmp_path):
    import yaml

    base = tmp_path / "config.yaml"
    base.write_text(BASE_CONFIG)
    path = reporter.job_config(str(base), {**CLAIMED, "data_source": "onprem"}, tmp_path / "jobs")
    assert yaml.safe_load(path.read_text())["job"]["source"] == "onprem"


ALIASED_CONFIG = BASE_CONFIG + "platform_data_targets: {onprem: onprem_return, aws: aws}\n"


@pytest.mark.parametrize(
    "override, expected",
    [
        (
            {
                "source_env": "aws",
                "target_env": "onprem",
                "data_source": "aws",
                "data_target": "onprem",
            },
            ("aws", "onprem_return"),
        ),
        ({"data_source": "onprem"}, ("onprem_return", "aws")),
        ({"data_source": "onprem_return"}, ("onprem_return", "aws")),
        ({"source_env": "rehearsal", "data_source": "onprem"}, ("onprem", "aws")),
    ],
)
def test_job_config_maps_environment_ids_to_runner_targets(tmp_path, override, expected):
    import yaml

    base = tmp_path / "config.yaml"
    base.write_text(ALIASED_CONFIG)
    path = reporter.job_config(str(base), {**CLAIMED, **override}, tmp_path / "jobs")
    job = yaml.safe_load(path.read_text())["job"]
    assert (job["source"], job["target"]) == expected
    assert job["live_phase_after"] == f"{expected[1]}_live"


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


COLLECTORS = {"aws": {"identifier": "db", "region": "r", "app_url": "http://app"}}


def test_policy_env_gives_each_job_its_own_evidence_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CUTOVER_POLICY_EVIDENCE", "/var/lib/worker/other/forward.json")
    monkeypatch.setenv("CUTOVER_POLICY_COLLECTOR_SETTINGS", "/var/lib/worker/other/settings.json")
    monkeypatch.delenv("CUTOVER_POLICY_COLLECTORS", raising=False)
    env = reporter.policy_env(CLAIMED, tmp_path)
    assert env["CUTOVER_POLICY_EVIDENCE"] == str(tmp_path / "policy" / "onprem_to_aws_001.json")
    assert "CUTOVER_POLICY_COLLECTOR_SETTINGS" not in env
    assert env["PATH"] == reporter.os.environ["PATH"]


def test_policy_env_writes_collector_settings_for_the_job_target(tmp_path, monkeypatch):
    collectors = tmp_path / "collectors.json"
    collectors.write_text(json.dumps(COLLECTORS))
    monkeypatch.setenv("CUTOVER_POLICY_COLLECTORS", str(collectors))
    env = reporter.policy_env(CLAIMED, tmp_path)
    settings = Path(env["CUTOVER_POLICY_COLLECTOR_SETTINGS"])
    assert settings == tmp_path / "policy" / "onprem_to_aws_001.settings.json"
    assert json.loads(settings.read_text()) == COLLECTORS["aws"]
    assert oct(settings.stat().st_mode & 0o777) == "0o600"


def test_policy_env_leaves_out_settings_for_targets_without_a_collector(tmp_path, monkeypatch):
    collectors = tmp_path / "collectors.json"
    collectors.write_text(json.dumps(COLLECTORS))
    monkeypatch.setenv("CUTOVER_POLICY_COLLECTORS", str(collectors))
    monkeypatch.setenv("CUTOVER_POLICY_COLLECTOR_SETTINGS", "/var/lib/worker/other/settings.json")
    env = reporter.policy_env({**CLAIMED, "data_target": "onprem_return"}, tmp_path)
    assert "CUTOVER_POLICY_COLLECTOR_SETTINGS" not in env
    assert env["CUTOVER_POLICY_EVIDENCE"].endswith("onprem_to_aws_001.json")


def test_policy_env_rejects_collectors_that_are_not_keyed_by_target(tmp_path, monkeypatch):
    collectors = tmp_path / "collectors.json"
    collectors.write_text("[]")
    monkeypatch.setenv("CUTOVER_POLICY_COLLECTORS", str(collectors))
    with pytest.raises(TypeError):
        reporter.policy_env(CLAIMED, tmp_path)


def test_execute_any_runs_the_tool_with_the_job_policy_env(tmp_path, monkeypatch):
    base = tmp_path / "config.yaml"
    base.write_text(BASE_CONFIG)
    collectors = tmp_path / "collectors.json"
    collectors.write_text(json.dumps(COLLECTORS))
    script = (
        "import os; print(os.environ['CUTOVER_POLICY_EVIDENCE']);"
        " print(os.environ['CUTOVER_POLICY_COLLECTOR_SETTINGS'])"
    )
    monkeypatch.setenv("ONEPORT_CUTOVER_ARGV", f"{sys.executable} -c {shlex.quote(script)}")
    monkeypatch.setenv("MIGRATION_CONFIG", str(base))
    monkeypatch.setenv("CUTOVER_POLICY_COLLECTORS", str(collectors))
    monkeypatch.setenv("CUTOVER_POLICY_EVIDENCE", "/var/lib/worker/other/forward.json")
    posted = []
    monkeypatch.setattr(reporter, "claim_any_command", lambda args, token: CLAIMED)
    monkeypatch.setattr(
        reporter, "post_json", lambda url, token, body: posted.append(body) or (200, body)
    )
    monkeypatch.setattr(reporter, "report_job", lambda args, token, job: None)
    args = Namespace(state_dir=str(tmp_path), platform_url="http://p", interval=5)
    assert reporter.execute_any(args, "token") is True
    tail = posted[-1]["log_tail"]
    assert posted[-1]["exit_code"] == 0
    assert str(tmp_path / "policy" / "onprem_to_aws_001.json") in tail
    assert str(tmp_path / "policy" / "onprem_to_aws_001.settings.json") in tail
    assert "other/forward.json" not in tail


BOT_METRICS = """# HELP traffic_bot_requests_total requests by state
traffic_bot_requests_total{state="pending"} 3
traffic_bot_requests_total{state="accepted_pending"} 1000
traffic_bot_requests_total{state="committed"} 83621
traffic_bot_requests_total{state="failed"} 5404
traffic_bot_attempts_total{kind="read_attempts"} 58634
traffic_bot_commit_latency_ms{quantile="0.95"} 42.5
traffic_bot_commit_latency_ms{quantile="0.99"} 90
traffic_bot_paused 0
"""

WRITER_CONFIG = """
bot: {control_url: "http://traffic-bot-control:9091"}
ingress:
  control_url: "http://durable-ingress-control:9092/"
  token_env: INGRESS_CONTROL_TOKEN
  targets: {onprem_return: onprem, aws: aws_db}
"""


def test_bot_metrics_reads_request_states_and_p95_latency():
    assert reporter.bot_metrics(BOT_METRICS) == {
        "pending": 3.0,
        "accepted_pending": 1000.0,
        "committed": 83621.0,
        "failed": 5404.0,
        "latency_p95": 42.5,
    }


def test_writer_report_names_the_ingress_target_with_the_job_target(tmp_path, monkeypatch):
    config = tmp_path / "job.yaml"
    config.write_text(WRITER_CONFIG)
    calls = []

    def fake_get(url, token=None):
        calls.append((url, token))
        if url.endswith("/metrics"):
            return BOT_METRICS
        return json.dumps({"target": "aws_db", "mode": "paused", "epoch": "5"})

    monkeypatch.setattr(reporter, "get_text", fake_get)
    monkeypatch.setenv("INGRESS_CONTROL_TOKEN", "secret")
    assert reporter.writer_report(config) == {
        "target": "aws",
        "mode": "paused",
        "committed": 83621,
        "failed": 5404,
        "pending": 3,
        "latency_p95_ms": 42.5,
    }
    assert calls == [
        ("http://traffic-bot-control:9091/metrics", None),
        ("http://durable-ingress-control:9092/status", "secret"),
    ]
    config.write_text("bot: {}\n")
    assert reporter.writer_report(config) is None


def test_job_writer_only_reports_jobs_that_are_still_moving(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "onprem_to_aws_001.yaml").write_text(WRITER_CONFIG)
    monkeypatch.setattr(reporter, "writer_report", lambda path: {"target": "aws"})
    running = {"stage": "SYNC_CONFIRMED"}
    assert reporter.job_writer(tmp_path, "onprem_to_aws_001", running) == {"target": "aws"}
    for stage in ("COMPLETED", "ROLLED_BACK", "ABORTED", "NEEDS_RECOVERY"):
        assert reporter.job_writer(tmp_path, "onprem_to_aws_001", {"stage": stage}) is None
    assert reporter.job_writer(tmp_path, "onprem_to_aws_001", None) is None
    assert reporter.job_writer(tmp_path, "other_job", running) is None

    def broken(path):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(reporter, "writer_report", broken)
    assert reporter.job_writer(tmp_path, "onprem_to_aws_001", running) is None


def test_report_job_sends_the_writer_with_the_cutover(tmp_path, monkeypatch):
    make_state(tmp_path)
    sent = []
    monkeypatch.setattr(reporter, "post", lambda url, token, body: sent.append(body) or 202)
    monkeypatch.setattr(
        reporter, "job_writer", lambda state, job, cutover: {"stage": cutover["stage"]}
    )
    args = Namespace(state_dir=str(tmp_path), platform_url="http://p", report_dir=None)
    reporter.report_job(args, "token", "forward_001")
    assert sent[0]["writer"] == {"stage": "READY"}
    monkeypatch.setattr(reporter, "job_writer", lambda state, job, cutover: None)
    sent.clear()
    reporter.report_job(args, "token", "forward_001")
    assert "writer" not in sent[0]


CLOUD_BASE = """
targets:
  aws_eks: {host: rds, dbname: shop}
  gcp_gke: {host: cloudsql, dbname: shop_b}
job: {job_id: placeholder, source: aws_eks, target: gcp_gke}
application:
  control_url: http://127.0.0.1:18080
  tables: [{name: public.migration_demo, primary_key: [request_id]}]
kafka: {connect_url: http://migration-connect:8083}
"""

CLOUD_CLAIM = {
    "command": {"id": "cmd_c", "action": "run", "approval_id": None},
    "job_id": "aws_to_gcp_001",
    "source_env": "aws",
    "target_env": "gcp",
    "data_source": "aws_eks",
    "data_target": "gcp_gke",
    "source_provider": "aws",
    "target_provider": "gcp",
}


def make_cloud_state(state_dir: Path, stage: str, failed: str | None = None) -> None:
    cloud = state_dir / "cloud"
    cloud.mkdir(parents=True, exist_ok=True)
    checkpoint = json.dumps({"failed_stage": failed} if failed else {})
    with sqlite3.connect(cloud / "cutover.sqlite3") as conn:
        conn.execute(
            "CREATE TABLE cutover_jobs (job_id TEXT, direction TEXT, source TEXT, target TEXT, stage TEXT, checkpoint TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO cutover_jobs VALUES ('aws_to_gcp_001','forward','aws_eks','gcp_gke',?,?,'2026-11-07T01:00:00Z')",
            (stage, checkpoint),
        )


@pytest.mark.parametrize(
    "providers, cloud",
    [
        (("aws", "gcp"), True),
        (("gcp", "aws"), True),
        (("onprem", "aws"), False),
        (("aws", "onprem"), False),
        ((None, "gcp"), False),
    ],
)
def test_only_cloud_to_cloud_tasks_take_the_cloud_route(providers, cloud):
    claimed = {**CLOUD_CLAIM, "source_provider": providers[0], "target_provider": providers[1]}
    assert reporter.is_cloud_route(claimed) is cloud


def test_cloud_job_config_keeps_the_application_and_swaps_the_job(tmp_path):
    import yaml

    base = tmp_path / "cloud.yaml"
    base.write_text(CLOUD_BASE)
    path = reporter.cloud_job_config(str(base), CLOUD_CLAIM, tmp_path / "jobs")
    config = yaml.safe_load(path.read_text())
    assert path.name == "aws_to_gcp_001.cloud.yaml"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert config["job"] == {"job_id": "aws_to_gcp_001", "source": "aws_eks", "target": "gcp_gke"}
    assert config["application"]["control_url"] == "http://127.0.0.1:18080"
    with pytest.raises(ValueError, match="gcp_x"):
        reporter.cloud_job_config(
            str(base), {**CLOUD_CLAIM, "data_target": "gcp_x"}, tmp_path / "jobs"
        )
    with pytest.raises(ValueError, match="MIGRATION_CLOUD_CONFIG"):
        reporter.cloud_job_config("", CLOUD_CLAIM, tmp_path / "jobs")


def test_cloud_argv_supports_run_approve_and_retry_only():
    base = ["python", "-m", "migration.cloud"]
    prefix = base + ["--config", "/c.yaml", "--state-dir", "/s/cloud"]
    argv = reporter.cloud_argv({"action": "run"}, base, "/c.yaml", "/s/cloud")
    assert argv == prefix + ["run"]
    assert reporter.cloud_argv(
        {"action": "approve", "approval_id": "A-1"}, base, "/c.yaml", "/s/cloud"
    ) == prefix + ["run", "--approve", "A-1"]
    assert reporter.cloud_argv({"action": "retry"}, base, "/c.yaml", "/s/cloud") == prefix + [
        "run",
        "--retry",
    ]
    for action in ("abort", "force_retry"):
        with pytest.raises(ValueError, match="지원하지 않는"):
            reporter.cloud_argv({"action": action}, base, "/c.yaml", "/s/cloud")
    with pytest.raises(ValueError, match="approval_id"):
        reporter.cloud_argv({"action": "approve"}, base, "/c.yaml", "/s/cloud")


@pytest.mark.parametrize(
    "stage, failed, reported, reported_failed",
    [
        ("COPYING", None, "COPYING", None),
        ("STOPPING", None, "SYNC_CONFIRMED", None),
        ("FAILED", "STOPPING", "ABORTED", "SYNC_CONFIRMED"),
        ("NEEDS_RECOVERY", "SWITCHING", "NEEDS_RECOVERY", "SWITCHING"),
    ],
)
def test_cloud_state_is_reported_in_platform_stages(
    tmp_path, stage, failed, reported, reported_failed
):
    make_cloud_state(tmp_path, stage, failed)
    cutover = reporter.read_job_cutover(tmp_path, "aws_to_gcp_001")
    assert cutover["stage"] == reported
    assert cutover["checkpoint"].get("failed_stage") == reported_failed
    assert reporter.known_jobs(tmp_path) == ["aws_to_gcp_001"]


def test_onprem_state_wins_and_both_states_are_listed(tmp_path):
    make_state(tmp_path)
    make_cloud_state(tmp_path, "COPYING")
    assert reporter.known_jobs(tmp_path) == ["aws_to_gcp_001", "forward_001"]
    assert reporter.read_job_cutover(tmp_path, "forward_001")["stage"] == "READY"


def test_adapter_runs_only_while_the_cloud_command_runs(tmp_path, monkeypatch):
    config = tmp_path / "c.yaml"
    script = tmp_path / "adapter.py"
    marker = tmp_path / "args.txt"
    script.write_text(
        "import socket, sys, time\n"
        f"open({str(marker)!r}, 'w').write(' '.join(sys.argv[1:]))\n"
        "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen()\n"
        "time.sleep(60)\n"
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config.write_text(CLOUD_BASE.replace("18080", str(port)))
    monkeypatch.setenv("ONEPORT_ADAPTER_ARGV", f"{sys.executable} {script} {port}")
    monkeypatch.setenv("CROSSCLOUD_INGRESS_ENV", "/etc/ingress.env")
    monkeypatch.setenv("CROSSCLOUD_INGRESS_CONTROL_URL", "http://ingress:9092")
    with reporter.adapter_running(config):
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    assert f"--cloud-config {config}" in marker.read_text()
    assert "--ingress-control-url http://ingress:9092" in marker.read_text()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()


def test_adapter_needs_its_ingress_settings(tmp_path, monkeypatch):
    config = tmp_path / "c.yaml"
    config.write_text(CLOUD_BASE)
    monkeypatch.delenv("CROSSCLOUD_INGRESS_ENV", raising=False)
    with pytest.raises(ValueError, match="CROSSCLOUD_INGRESS_ENV"):
        with reporter.adapter_running(config):
            pass


def test_adapter_that_exits_early_is_a_config_error(tmp_path, monkeypatch):
    config = tmp_path / "c.yaml"
    config.write_text(CLOUD_BASE)
    monkeypatch.setenv("ONEPORT_ADAPTER_ARGV", f"{sys.executable} -c 'raise SystemExit(4)'")
    monkeypatch.setenv("CROSSCLOUD_INGRESS_ENV", "/etc/ingress.env")
    monkeypatch.setenv("CROSSCLOUD_INGRESS_CONTROL_URL", "http://ingress:9092")
    with pytest.raises(ValueError, match="exit 4"):
        with reporter.adapter_running(config):
            pass


def test_execute_any_runs_cloud_tasks_with_migration_cloud(tmp_path, monkeypatch):
    base = tmp_path / "cloud.yaml"
    base.write_text(CLOUD_BASE)
    script = "import sys; print(' '.join(sys.argv[1:])); sys.exit(3)"
    monkeypatch.setenv("ONEPORT_CLOUD_ARGV", f"{sys.executable} -c {shlex.quote(script)}")
    monkeypatch.setenv("MIGRATION_CLOUD_CONFIG", str(base))
    entered = []

    @reporter.contextmanager
    def fake_adapter(config):
        entered.append(config)
        yield

    posted = []
    monkeypatch.setattr(reporter, "adapter_running", fake_adapter)
    monkeypatch.setattr(reporter, "claim_any_command", lambda args, token: CLOUD_CLAIM)
    monkeypatch.setattr(
        reporter, "post_json", lambda url, token, body: posted.append((url, body)) or (200, body)
    )
    monkeypatch.setattr(reporter, "report_job", lambda args, token, job: None)
    args = Namespace(state_dir=str(tmp_path), platform_url="http://p/", interval=5)
    assert reporter.execute_any(args, "token") is True
    url, body = posted[-1]
    assert url.endswith("/migrations/aws_to_gcp_001/commands/cmd_c/result")
    assert body["exit_code"] == 3
    assert f"--state-dir {tmp_path / 'cloud'} run" in body["log_tail"]
    assert entered == [tmp_path / "jobs" / "aws_to_gcp_001.cloud.yaml"]


def approve_setup(tmp_path, monkeypatch, status_exit):
    base = tmp_path / "cloud.yaml"
    base.write_text(CLOUD_BASE)
    calls = tmp_path / "calls.txt"
    tool = (
        "import sys; open(sys.argv[1], 'a').write(sys.argv[2] + ' ' + ' '.join(sys.argv[3:]) + '\\n');"
        " sys.exit(int(sys.argv[2] == 'status') * {code})"
    ).format(code=status_exit)
    quoted = shlex.quote(tool)
    monkeypatch.setenv("ONEPORT_CLOUD_ARGV", f"{sys.executable} -c {quoted} {calls} cloud")
    monkeypatch.setenv(
        "ONEPORT_INGRESS_STATUS_ARGV", f"{sys.executable} -c {quoted} {calls} status"
    )
    monkeypatch.setenv("MIGRATION_CLOUD_CONFIG", str(base))
    monkeypatch.setenv("CROSSCLOUD_INGRESS_ENV", "/etc/ingress.env")
    monkeypatch.setenv("CROSSCLOUD_INGRESS_CONTROL_URL", "http://ingress:9092")

    @reporter.contextmanager
    def fake_adapter(config):
        yield

    posted = []
    claim = {**CLOUD_CLAIM, "command": {"id": "cmd_a", "action": "approve", "approval_id": "ok-1"}}
    monkeypatch.setattr(reporter, "adapter_running", fake_adapter)
    monkeypatch.setattr(reporter, "claim_any_command", lambda args, token: claim)
    monkeypatch.setattr(
        reporter, "post_json", lambda url, token, body: posted.append(body) or (200, body)
    )
    monkeypatch.setattr(reporter, "report_job", lambda args, token, job: None)
    args = Namespace(state_dir=str(tmp_path), platform_url="http://p", interval=5)
    assert reporter.execute_any(args, "token") is True
    return calls.read_text().splitlines(), posted[-1]


def test_cloud_approve_checks_the_ingress_queue_first(tmp_path, monkeypatch):
    calls, result = approve_setup(tmp_path, monkeypatch, 0)
    assert [line.split()[0] for line in calls] == ["status", "cloud"]
    assert f"--state-dir {tmp_path / 'cloud'}" in calls[0]
    assert "--ingress-control-url http://ingress:9092" in calls[0]
    assert calls[1].endswith("run --approve ok-1")
    assert result["exit_code"] == 0


def test_cloud_approve_stops_when_the_ingress_queue_is_not_ready(tmp_path, monkeypatch):
    calls, result = approve_setup(tmp_path, monkeypatch, 4)
    assert [line.split()[0] for line in calls] == ["status"]
    assert result["exit_code"] == 4
    assert "ingress-status" in result["log_tail"]
