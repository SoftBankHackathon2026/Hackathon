from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from agent import __main__ as cli
from agent import runs as R
from agent.config import parse_config
from agent.platform import PlatformClient, handle_approvals
from agent.runs import RunStore


class FakeOpener:
    def __init__(self, responses: dict | None = None) -> None:
        self.requests: list[tuple[str, str, dict | None, str]] = []
        self.responses = responses or {}

    def open(self, req, timeout):
        body = json.loads(req.data) if req.data else None
        path = req.full_url.split("://", 1)[1].split("/", 1)[1]
        self.requests.append((req.get_method(), "/" + path, body, req.get_header("Authorization")))
        payload = self.responses.get(("/" + path).split("?")[0], b"")
        return _Resp(payload)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def client(opener: FakeOpener) -> PlatformClient:
    return PlatformClient("https://platform.example/", "ingest-secret", "oneport-agent", opener=opener)


def test_heartbeat_and_auth_header() -> None:
    opener = FakeOpener()
    client(opener).heartbeat(version="0.1.0", auto_apply=False, interval_seconds=60)
    method, path, body, auth = opener.requests[0]
    assert (method, path, auth) == ("POST", "/api/v1/ingest/agent/heartbeat", "Bearer ingest-secret")
    assert body["agent_id"] == "oneport-agent" and body["auto_apply"] is False and body["interval_seconds"] == 60
    assert "ingest-secret" not in repr(client(opener))


def test_sync_pushes_only_changed_runs(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    first = runs.create("job")
    runs.update(first, R.WAITING_APPROVAL, "승인 대기", proposal={"changes": [{"key": "DB_NAME", "value": "x"}]})
    opener = FakeOpener()
    c = client(opener)
    assert c.sync(runs) == 1
    method, path, body, _ = opener.requests[0]
    assert (method, path) == ("PUT", f"/api/v1/ingest/agent/runs/{first.run_id}")
    assert body["state"] == R.WAITING_APPROVAL and body["agent_id"] == "oneport-agent" and "run_id" not in body
    assert body["events"][-1]["message"] == "승인 대기"
    assert c.sync(runs) == 0
    runs.update(first, R.RESOLVED, "끝")
    assert c.sync(runs) == 1
    runs.close()


class StubRemediator:
    def __init__(self, runs: RunStore) -> None:
        self.runs, self.calls = runs, []

    def approve(self, run_id, by=None):
        self.calls.append(("approve", run_id, by))
        run = self.runs.get(run_id)
        if run is None or run.state != R.WAITING_APPROVAL:
            raise ValueError("not waiting")
        self.runs.update(run, R.RESOLVED, "끝")
        return run

    def reject(self, run_id, by=None):
        self.calls.append(("reject", run_id, by))
        if not self.runs.claim_rejection(run_id, by):
            raise ValueError("not waiting")
        return self.runs.get(run_id)


def test_handle_approvals_applies_and_acks(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    a, b, c_run = runs.create("job"), runs.create("job"), runs.create("job")
    for run in (a, b):
        runs.update(run, R.WAITING_APPROVAL, "승인 대기")
    runs.update(c_run, R.RESOLVED, "끝")
    opener = FakeOpener({"/api/v1/ingest/agent/approvals": json.dumps([
        {"id": 1, "run_id": a.run_id, "decision": "approve", "requested_by": "유빈", "requested_at": "t"},
        {"id": 2, "run_id": b.run_id, "decision": "reject", "requested_by": "유빈", "requested_at": "t"},
        {"id": 3, "run_id": c_run.run_id, "decision": "approve", "requested_by": "유빈", "requested_at": "t"},
    ]).encode()})
    stub = StubRemediator(runs)
    handled = handle_approvals(client(opener), stub, runs)
    assert handled == [(a.run_id, R.RESOLVED), (b.run_id, R.REJECTED), (c_run.run_id, R.RESOLVED)]
    acks = [(p, body) for m, p, body, _ in opener.requests if p.endswith("/ack")]
    assert acks == [
        ("/api/v1/ingest/agent/approvals/1/ack", {"result_state": R.RESOLVED}),
        ("/api/v1/ingest/agent/approvals/2/ack", {"result_state": R.REJECTED}),
        ("/api/v1/ingest/agent/approvals/3/ack", {"result_state": R.RESOLVED}),
    ]
    assert stub.calls[0] == ("approve", a.run_id, "유빈, 플랫폼")
    assert "사람이 거절함 (유빈, 플랫폼)" in [e["message"] for e in runs.events(b.run_id)]
    runs.close()


def test_platform_errors_do_not_raise(tmp_path: Path) -> None:
    class Down:
        def open(self, req, timeout):
            raise OSError("down")

    c = PlatformClient("https://platform.example", "t", "a", opener=Down())
    c.heartbeat(version="v", auto_apply=True, interval_seconds=60)
    assert c.approvals() == []
    runs = RunStore(tmp_path)
    runs.create("job")
    assert c.sync(runs) == 0
    runs.close()


@pytest.mark.parametrize("url,agent", [("ftp://x", "a"), ("https://x", "bad id!")])
def test_invalid_platform_settings_rejected(url: str, agent: str) -> None:
    with pytest.raises(ValueError):
        PlatformClient(url, "t", agent, opener=FakeOpener())


def test_build_platform_needs_url_and_token() -> None:
    raw = {
        "job_id": "j", "policy": {"opa_url": "http://opa"}, "plan": {"target_db": {"host": "h", "port": 5432}},
        "declared_evidence": {"target_db_exposure": {"public": False}, "target_location": {"location": "l"}},
        "smoke": {"target_only": True, "target_url": "http://t"},
        "openbao": {"addr": "http://bao", "mount": "secret", "path": "p"},
        "platform": {"url": "https://platform.example", "agent_id": "vm-1"},
    }
    cfg = parse_config(raw, {"VAULT_TOKEN": "t"})
    assert cli.build_platform(cfg, {}) is None
    built = cli.build_platform(cfg, {"ONEPORT_INGEST_TOKEN": "x"})
    assert built is not None and built.agent_id == "vm-1"


def test_watch_heartbeats_handles_approvals_and_syncs(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    opener = FakeOpener()

    class Rem:
        auto_apply = False

        def run(self):
            run = runs.create("job")
            runs.update(run, R.HANDED_OFF, "사람 확인 필요")
            return run

    cli.watch(Rem(), runs, interval=60, iterations=1, sleep=lambda _: None, platform=client(opener))
    paths = [p.split("?")[0] for _, p, _, _ in opener.requests]
    assert paths[0] == "/api/v1/ingest/agent/heartbeat"
    assert "/api/v1/ingest/agent/approvals" in paths
    assert any(p.startswith("/api/v1/ingest/agent/runs/run-") for p in paths)
    runs.close()


def test_waiting_message_links_platform(make_world) -> None:
    w = make_world(auto_apply=False)
    w.remediator.approve_url = "https://platform.example/"
    run = w.remediator.run()
    waiting = next(n for n in w.notes if n.startswith("🟡"))
    assert f"<https://platform.example/#agent/{run.run_id}|플랫폼에서 승인>" in waiting


def test_human_reject_keeps_store_and_notifies(make_world) -> None:
    w = make_world(auto_apply=False)
    run = w.remediator.run()
    rejected = w.remediator.reject(run.run_id, by="유빈")
    assert rejected.state == R.REJECTED and w.kv.writes == [] and w.restarter.count == 0
    assert w.notes[-1].startswith("🚫") and "유빈 님이 제안을 거절했어요" in w.notes[-1]
    with pytest.raises(ValueError):
        w.remediator.reject(run.run_id)


def test_sync_skips_nothing_to_fix(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    quiet = runs.create("job")
    runs.update(quiet, R.NOTHING_TO_FIX, "정상")
    opener = FakeOpener()
    assert client(opener).sync(runs) == 0 and opener.requests == []
    runs.close()


def test_recorded_proposal_keeps_before_value(make_world) -> None:
    w = make_world(auto_apply=False)
    run = w.remediator.run()
    change = run.detail["proposal"]["changes"][0]
    assert change["key"] == "DB_HOST" and change["before"] and change["before"] != change["value"]
    assert w.remediator.approve(run.run_id).state == "RESOLVED"


def test_sync_reaches_older_runs_behind_quiet_ones(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    fixed = runs.create("job")
    runs.update(fixed, R.RESOLVED, "끝")
    for _ in range(30):
        quiet = runs.create("job")
        runs.update(quiet, R.NOTHING_TO_FIX, "정상")
    opener = FakeOpener()
    assert client(opener).sync(runs) == 1
    assert opener.requests[0][1] == f"/api/v1/ingest/agent/runs/{fixed.run_id}"
    assert [r.run_id for r in runs.recent(5)][0] != fixed.run_id
    runs.close()

