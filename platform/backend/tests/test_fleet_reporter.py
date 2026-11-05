import importlib.util
import json
import sqlite3
from argparse import Namespace
from pathlib import Path

path = Path(__file__).parents[2] / "ops" / "fleet_reporter.py"
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
