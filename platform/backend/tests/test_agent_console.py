from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from controlplane.core.errors import RequestConflict
from controlplane.main import create_app
from controlplane.providers.registry import ProviderRegistry
from controlplane.repositories.agent import AgentJournal
from controlplane.schemas.agent import AgentHeartbeat, RunReport, secret_keys
from controlplane.services.agent import AgentService, is_online, summarize

TOKEN = "ingest-test-token-0123456789-abcdef"
ORIGIN = {"Origin": "http://127.0.0.1:4320"}
BEARER = {"Authorization": "Bearer " + TOKEN}
PASSWORD = "correct-horse-battery"
RUN = "run-0a1b2c3d"
BASE = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


def at(seconds: int) -> str:
    return (BASE + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def run_body(state="WAITING_APPROVAL", updated=10, agent_id="agent-a", **extra):
    return {
        "agent_id": agent_id,
        "job_id": "forward_001",
        "state": state,
        "created_at": at(0),
        "updated_at": at(updated),
        "detail": {
            "pre_decision": {"outcome": "deny", "reason_codes": ["APP-01"]},
            "proposal": {
                "changes": [{"key": "DB_HOST", "value": "db.example.internal"}],
                "cause": "c",
                "impact": "i",
                "verification": "v",
            },
            "diagnosis": {"detail": "DB 주소가 원본을 가리켜요."},
        },
        "events": [{"at": at(updated), "state": state, "message": "승인 대기"}],
        **extra,
    }


def beat(**extra):
    return {
        "agent_id": "agent-a",
        "version": "0.3.0",
        "auto_apply": False,
        "interval_seconds": 30,
        "observed_at": at(0),
        **extra,
    }


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("ONEPORT_CONFIG", raising=False)
    monkeypatch.delenv("ONEPORT_RUNTIME_CONFIG", raising=False)
    monkeypatch.delenv("ONEPORT_FLEET_CONFIG", raising=False)
    monkeypatch.setenv("ONEPORT_INGEST_TOKEN", TOKEN)


@pytest.fixture
def client(env, store):
    app = create_app(registry=ProviderRegistry(), store=store, auth_mode="demo")
    with TestClient(app) as client:
        yield client


def put(client, body, run_id=RUN, headers=BEARER):
    return client.put(f"/api/v1/ingest/agent/runs/{run_id}", json=body, headers=headers)


def test_summary_reads_reason_codes_changes_and_diagnosis():
    assert summarize(run_body()["detail"]).model_dump() == {
        "reason_codes": ["APP-01"],
        "changes": [{"key": "DB_HOST", "value": "db.example.internal"}],
        "diagnosis": "DB 주소가 원본을 가리켜요.",
    }
    assert summarize(
        {"proposal": "x", "pre_decision": {"reason_codes": "APP-01"}}
    ).model_dump() == {
        "reason_codes": [],
        "changes": [],
        "diagnosis": None,
    }


def test_secret_keys_are_found_at_any_depth():
    assert secret_keys({"a": [{"b": {"DB_Password": 1}}]}) == ["DB_Password"]
    assert secret_keys({"x": {"vault_token_ttl": 1}, "y": [{"conn_dsn": ""}]}) == [
        "vault_token_ttl",
        "conn_dsn",
    ]
    assert secret_keys({"secretary": 1}) == ["secretary"]
    assert secret_keys({"proposal": {"changes": [{"key": "DB_HOST"}]}}) == []


def test_online_window_is_three_intervals_with_a_floor():
    assert is_online(BASE, 60, BASE + timedelta(seconds=180))
    assert not is_online(BASE, 60, BASE + timedelta(seconds=181))
    assert is_online(BASE, 10, BASE + timedelta(seconds=90))
    assert not is_online(BASE, 10, BASE + timedelta(seconds=91))


def test_ingest_requires_token_and_database(env, client):
    assert client.post("/api/v1/ingest/agent/heartbeat", json=beat()).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert put(client, run_body(), headers=wrong).status_code == 401
    assert client.get("/api/v1/ingest/agent/approvals?agent_id=agent-a").status_code == 401
    assert (
        client.post("/api/v1/ingest/agent/approvals/1/ack", json={"result_state": "X"}).status_code
        == 401
    )
    with TestClient(create_app(registry=ProviderRegistry(), auth_mode="demo")) as bare:
        assert (
            bare.post("/api/v1/ingest/agent/heartbeat", json=beat(), headers=BEARER).status_code
            == 503
        )
        assert bare.get("/api/v1/agent/runs").status_code == 503


@pytest.mark.parametrize(
    "change",
    [
        {"agent_id": "bad id"},
        {"agent_id": "x" * 65},
        {"version": "v" * 65},
        {"interval_seconds": 9},
        {"interval_seconds": 3601},
        {"observed_at": "yesterday"},
    ],
)
def test_heartbeat_validation(client, change):
    response = client.post("/api/v1/ingest/agent/heartbeat", json=beat(**change), headers=BEARER)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "change",
    [
        {"state": "DONE"},
        {"job_id": "j" * 129},
        {"agent_id": "a/b"},
        {"events": [{"at": at(0), "state": "X", "message": "m" * 2001}]},
        {"events": [{"at": at(0), "state": "X", "message": "m"}] * 201},
        {"detail": {"blob": "x" * (64 * 1024)}},
        {"detail": {"probe": {"Target_DSN": "postgresql://example"}}},
        {"detail": {"proposal": {"changes": [{"key": "K", "value": "v", "db_password": "p"}]}}},
        {"detail": {"codex": {"access_token": "t"}}},
        {"detail": {"SECRET_REF": "s"}},
    ],
)
def test_run_report_validation_rejects_bad_shapes_and_secret_keys(client, change):
    assert put(client, {**run_body(), **change}).status_code == 422


def test_run_id_must_match_pattern(client):
    assert put(client, run_body(), run_id="run-XYZ").status_code == 422
    assert put(client, run_body(), run_id="run-abc").status_code == 422
    assert put(client, run_body(), run_id="job-0a1b2c3d").status_code == 422


def test_heartbeat_upserts_and_status_reports_online(client):
    assert client.get("/api/v1/agent/status").json() == {"agents": []}
    assert (
        client.post("/api/v1/ingest/agent/heartbeat", json=beat(), headers=BEARER).status_code
        == 204
    )
    newer = beat(version="0.4.0", auto_apply=True, interval_seconds=60, observed_at=at(30))
    assert (
        client.post("/api/v1/ingest/agent/heartbeat", json=newer, headers=BEARER).status_code == 204
    )
    agents = client.get("/api/v1/agent/status").json()["agents"]
    assert len(agents) == 1
    agent = agents[0]
    assert agent["agent_id"] == "agent-a" and agent["version"] == "0.4.0"
    assert agent["auto_apply"] is True and agent["interval_seconds"] == 60
    assert datetime.fromisoformat(agent["observed_at"]) == BASE + timedelta(seconds=30)
    assert agent["online"] is True and agent["received_at"]


def test_status_marks_stale_agents_offline(store):
    clock = [BASE]
    service = AgentService(AgentJournal(store), now=lambda: clock[0])
    service.heartbeat(AgentHeartbeat.model_validate(beat()))
    clock[0] = BASE + timedelta(seconds=90)
    assert service.status().agents[0].online is True
    clock[0] = BASE + timedelta(seconds=91)
    assert service.status().agents[0].online is False


def test_run_upsert_keeps_the_latest_update(client):
    assert put(client, run_body(state="CHECKING", updated=5)).status_code == 204
    assert put(client, run_body(state="WAITING_APPROVAL", updated=10)).status_code == 204
    assert put(client, run_body(state="CHECKING", updated=7)).status_code == 204
    detail = client.get(f"/api/v1/agent/runs/{RUN}").json()
    assert detail["state"] == "WAITING_APPROVAL"
    assert datetime.fromisoformat(detail["updated_at"]) == BASE + timedelta(seconds=10)
    assert datetime.fromisoformat(detail["created_at"]) == BASE
    assert detail["events"] == [{"at": at(10), "state": "WAITING_APPROVAL", "message": "승인 대기"}]
    assert detail["detail"]["proposal"]["cause"] == "c"
    assert put(client, run_body(state="RESOLVED", updated=10)).status_code == 204
    assert client.get(f"/api/v1/agent/runs/{RUN}").json()["state"] == "RESOLVED"


def test_run_list_is_newest_first_with_summary_and_limit(client):
    for index, run_id in enumerate(["run-000001", "run-000002", "run-000003"]):
        body = {**run_body(state="NOTHING_TO_FIX"), "created_at": at(index * 60)}
        assert put(client, body, run_id=run_id).status_code == 204
    listed = client.get("/api/v1/agent/runs").json()["runs"]
    assert [r["run_id"] for r in listed] == ["run-000003", "run-000002", "run-000001"]
    assert listed[0]["summary"] == {
        "reason_codes": ["APP-01"],
        "changes": [{"key": "DB_HOST", "value": "db.example.internal"}],
        "diagnosis": "DB 주소가 원본을 가리켜요.",
    }
    assert "detail" not in listed[0] and "events" not in listed[0]
    assert len(client.get("/api/v1/agent/runs?limit=2").json()["runs"]) == 2
    assert client.get("/api/v1/agent/runs?limit=0").status_code == 422
    assert client.get("/api/v1/agent/runs?limit=201").status_code == 422
    assert client.get("/api/v1/agent/runs/run-ffffff").status_code == 404


def test_approval_flow_conflicts_and_ack(client):
    approve = f"/api/v1/agent/runs/{RUN}/approve"
    reject = f"/api/v1/agent/runs/{RUN}/reject"
    assert client.post(approve, headers=ORIGIN).status_code == 404
    assert put(client, run_body(state="CHECKING", updated=5)).status_code == 204
    assert client.post(approve, headers=ORIGIN).status_code == 409
    assert client.get(f"/api/v1/agent/runs/{RUN}").json()["can_decide"] is False

    assert put(client, run_body()).status_code == 204
    detail = client.get(f"/api/v1/agent/runs/{RUN}").json()
    assert detail["can_decide"] is True and detail["pending_approval"] is None
    assert client.post(approve).status_code == 403
    assert client.post(approve, headers={"Origin": "https://evil.example"}).status_code == 403

    accepted = client.post(approve, headers=ORIGIN)
    assert accepted.status_code == 202
    ticket = accepted.json()
    assert ticket["decision"] == "approve" and isinstance(ticket["id"], int)
    assert client.post(reject, headers=ORIGIN).status_code == 409
    assert client.post(approve, headers=ORIGIN).status_code == 409
    detail = client.get(f"/api/v1/agent/runs/{RUN}").json()
    assert detail["can_decide"] is False
    assert detail["pending_approval"]["id"] == ticket["id"]
    assert detail["pending_approval"]["requested_by"] == "운영 화면"

    queue = "/api/v1/ingest/agent/approvals"
    assert client.get(queue, params={"agent_id": "agent-b"}, headers=BEARER).json() == []
    assert client.get(queue, params={"agent_id": "bad id"}, headers=BEARER).status_code == 422
    pending = client.get(queue, params={"agent_id": "agent-a"}, headers=BEARER).json()
    assert [(p["id"], p["run_id"], p["decision"]) for p in pending] == [
        (ticket["id"], RUN, "approve")
    ]
    assert pending[0]["requested_by"] == "운영 화면" and pending[0]["requested_at"]

    ack = f"/api/v1/ingest/agent/approvals/{ticket['id']}/ack"
    assert client.post(ack, json={"result_state": "R" * 33}, headers=BEARER).status_code == 422
    assert client.post(ack, json={"result_state": "APPLYING"}, headers=BEARER).status_code == 204
    assert client.post(ack, json={"result_state": "OTHER"}, headers=BEARER).status_code == 204
    missing = "/api/v1/ingest/agent/approvals/999999/ack"
    assert client.post(missing, json={"result_state": "X"}, headers=BEARER).status_code == 404
    assert client.get(queue, params={"agent_id": "agent-a"}, headers=BEARER).json() == []
    assert client.get(f"/api/v1/agent/runs/{RUN}").json()["pending_approval"] is None

    assert put(client, run_body(state="REJECTED", updated=20)).status_code == 204
    assert client.post(reject, headers=ORIGIN).status_code == 409


def test_ack_keeps_the_first_result(store):
    journal = AgentJournal(store)
    service = AgentService(journal, now=lambda: BASE)
    service.report(RUN, RunReport.model_validate(run_body()))
    ticket = service.decide(RUN, "reject", None)
    service.ack(ticket.id, "REJECTED")
    service.ack(ticket.id, "APPLYING")
    with journal.connect() as conn:
        row = conn.execute(
            "SELECT result_state, consumed_at FROM agent_approvals WHERE id=%s", (ticket.id,)
        ).fetchone()
    assert row["result_state"] == "REJECTED" and row["consumed_at"] == BASE


def test_open_approval_index_blocks_a_second_decision(store):
    journal = AgentJournal(store)
    journal.put_run(RUN, RunReport.model_validate(run_body()))
    journal.decide(RUN, "approve", "a", BASE)
    with pytest.raises(RequestConflict):
        journal.decide(RUN, "reject", "b", BASE)


def test_approvals_are_oldest_first_across_runs(client):
    for run_id in ("run-00000a", "run-00000b"):
        assert put(client, run_body(), run_id=run_id).status_code == 204
    second = client.post("/api/v1/agent/runs/run-00000b/reject", headers=ORIGIN).json()
    first = client.post("/api/v1/agent/runs/run-00000a/approve", headers=ORIGIN).json()
    pending = client.get(
        "/api/v1/ingest/agent/approvals", params={"agent_id": "agent-a"}, headers=BEARER
    ).json()
    assert [p["id"] for p in pending] == [second["id"], first["id"]]


@pytest.fixture
def login_app(env, store):
    return create_app(registry=ProviderRegistry(), store=store, auth_mode="login")


def signed_in(app, email, team):
    client = TestClient(app, headers=ORIGIN)
    client.__enter__()
    created = client.post(
        "/api/v1/auth/signup", json={"email": email, "password": PASSWORD, "team": team}
    )
    assert created.status_code == 201, created.text
    return client


def test_login_mode_requires_session_and_deploy_role(login_app):
    with TestClient(login_app, headers=ORIGIN) as anonymous:
        assert anonymous.get("/api/v1/agent/status").status_code == 401
        assert anonymous.get("/api/v1/agent/runs").status_code == 401
        assert put(anonymous, run_body()).status_code == 204
        assert anonymous.post(f"/api/v1/agent/runs/{RUN}/approve").status_code == 401
    owner = signed_in(login_app, "owner@example.com", "platform")
    viewer = signed_in(login_app, "viewer@example.com", "platform")
    try:
        assert viewer.get(f"/api/v1/agent/runs/{RUN}").json()["can_decide"] is False
        assert viewer.post(f"/api/v1/agent/runs/{RUN}/approve").status_code == 403
        assert owner.get(f"/api/v1/agent/runs/{RUN}").json()["can_decide"] is True
        accepted = owner.post(f"/api/v1/agent/runs/{RUN}/reject")
        assert accepted.status_code == 202 and accepted.json()["decision"] == "reject"
        detail = owner.get(f"/api/v1/agent/runs/{RUN}").json()
        assert detail["pending_approval"]["requested_by"] == "owner@example.com"
        assert detail["can_decide"] is False
    finally:
        owner.__exit__(None, None, None)
        viewer.__exit__(None, None, None)
