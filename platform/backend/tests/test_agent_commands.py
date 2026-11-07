import pytest
from fastapi.testclient import TestClient
from test_agent_console import BASE, BEARER, ORIGIN, PASSWORD, RUN, beat, put, run_body
from test_agent_console import client as client
from test_agent_console import env as env

from controlplane.core.errors import RequestConflict
from controlplane.main import create_app
from controlplane.providers.registry import ProviderRegistry
from controlplane.repositories.agent import AgentJournal
from controlplane.schemas.agent import AgentHeartbeat
from controlplane.services.agent import AgentService

CONFIG = {
    "auto_apply": False,
    "interval_seconds": 60,
    "job_id": "forward_001",
    "stage": "pre_cutover",
    "slack": {"mode": "bot", "enabled": True, "channel": "C000TEST", "thread_ts": None},
    "fix": {"keys": ["DB_HOST", "DB_PORT"], "plan": {"DB_HOST": "db.example.internal"}},
    "sandbox": {"runner": "opensandbox", "image": "codex:test", "model": None},
    "settings_applied_at": None,
}
HEARTBEAT = "/api/v1/ingest/agent/heartbeat"
SETTINGS = "/api/v1/agent/agents/agent-a/settings"
RERUN = "/api/v1/agent/agents/agent-a/rerun"
QUEUE = "/api/v1/ingest/agent/commands"


def send_beat(client, **extra):
    response = client.post(HEARTBEAT, json=beat(**extra), headers=BEARER)
    assert response.status_code == 204, response.text
    return response


def agent(client):
    return client.get("/api/v1/agent/status").json()["agents"][0]


def queue(client, agent_id="agent-a"):
    return client.get(QUEUE, params={"agent_id": agent_id}, headers=BEARER)


def test_heartbeat_keeps_the_latest_config(client):
    send_beat(client)
    assert agent(client)["config"] is None and agent(client)["pending_commands"] == []
    send_beat(client, config=CONFIG)
    assert agent(client)["config"] == CONFIG
    send_beat(client)
    assert agent(client)["config"] == CONFIG
    changed = {**CONFIG, "auto_apply": True}
    send_beat(client, config=changed)
    assert agent(client)["config"]["auto_apply"] is True


@pytest.mark.parametrize(
    "config",
    [
        {"slack": {"webhook_url": "https://hooks.example/x"}},
        {"slack": {"Bot_Token": "x"}},
        {"fix": {"plan": {"DB_PASSWORD": "x"}}},
        {"items": [{"target_dsn": "x"}]},
        {"client_secret": "x"},
        {"blob": "x" * (16 * 1024)},
    ],
)
def test_heartbeat_config_rejects_credentials_and_size(client, config):
    assert client.post(HEARTBEAT, json=beat(config=config), headers=BEARER).status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"auto_apply": None},
        {"auto_apply": "yes"},
        {"interval_seconds": 29},
        {"interval_seconds": 3601},
        {"interval_seconds": "60"},
        {"slack_enabled": 1},
        {"slack_thread_ts": "12345.1"},
        {"slack_thread_ts": "1234567.1234567890"},
        {"slack_thread_ts": "abc"},
        {"slack_channel": "C1"},
        {"auto_apply": True, "job_id": "x"},
        [],
    ],
)
def test_settings_validation(client, body):
    send_beat(client)
    assert client.post(SETTINGS, json=body, headers=ORIGIN).status_code == 422


def test_settings_and_rerun_need_a_known_agent_and_screen_origin(client):
    assert client.post(SETTINGS, json={"auto_apply": True}, headers=ORIGIN).status_code == 404
    assert client.post(RERUN, headers=ORIGIN).status_code == 404
    assert client.post("/api/v1/agent/agents/bad id/rerun", headers=ORIGIN).status_code == 422
    send_beat(client)
    assert client.post(SETTINGS, json={"auto_apply": True}).status_code == 403
    assert client.post(RERUN, headers={"Origin": "https://evil.example"}).status_code == 403


def test_command_queue_flow(client):
    send_beat(client)
    first = client.post(
        SETTINGS,
        json={"auto_apply": True, "slack_thread_ts": "1730000000.123456"},
        headers=ORIGIN,
    )
    assert first.status_code == 202 and first.json()["kind"] == "settings"
    second = client.post(
        SETTINGS, json={"interval_seconds": 120, "slack_thread_ts": ""}, headers=ORIGIN
    )
    assert second.status_code == 202
    rerun = client.post(RERUN, headers=ORIGIN)
    assert rerun.status_code == 202 and rerun.json()["kind"] == "rerun"
    assert client.post(RERUN, headers=ORIGIN).status_code == 409

    pending = queue(client).json()
    assert [(c["id"], c["kind"]) for c in pending] == [
        (first.json()["id"], "settings"),
        (second.json()["id"], "settings"),
        (rerun.json()["id"], "rerun"),
    ]
    assert pending[0]["payload"] == {"auto_apply": True, "slack_thread_ts": "1730000000.123456"}
    assert pending[1]["payload"] == {"interval_seconds": 120, "slack_thread_ts": ""}
    assert pending[2]["payload"] == {}
    assert pending[0]["requested_by"] == "운영 화면" and pending[0]["requested_at"]
    assert agent(client)["pending_commands"] == pending
    assert queue(client, "agent-b").json() == []
    assert queue(client, "bad id").status_code == 422

    ack = f"{QUEUE}/{rerun.json()['id']}/ack"
    assert client.post(ack, json={"result": "x" * 201}, headers=BEARER).status_code == 422
    assert client.post(ack, json={"result": "started"}, headers=BEARER).status_code == 204
    assert client.post(ack, json={"result": "again"}, headers=BEARER).status_code == 204
    missing = f"{QUEUE}/999999/ack"
    assert client.post(missing, json={"result": "x"}, headers=BEARER).status_code == 404
    assert [c["kind"] for c in queue(client).json()] == ["settings", "settings"]
    assert client.post(RERUN, headers=ORIGIN).status_code == 202


def test_command_ingest_requires_token(client):
    assert client.get(QUEUE, params={"agent_id": "agent-a"}).status_code == 401
    wrong = {"Authorization": "Bearer nope"}
    assert client.post(f"{QUEUE}/1/ack", json={"result": "x"}, headers=wrong).status_code == 401


def test_command_ack_keeps_the_first_result(store):
    journal = AgentJournal(store)
    service = AgentService(journal)
    service.heartbeat(AgentHeartbeat.model_validate(beat()))
    command = service.rerun("agent-a", None)
    service.ack_command(command.id, "started")
    service.ack_command(command.id, "other")
    with journal.connect() as conn:
        row = conn.execute(
            "SELECT result, consumed_at FROM agent_commands WHERE id=%s", (command.id,)
        ).fetchone()
    assert row["result"] == "started" and row["consumed_at"] is not None


def test_open_rerun_index_blocks_a_second_rerun(store):
    journal = AgentJournal(store)
    AgentService(journal).heartbeat(AgentHeartbeat.model_validate(beat()))
    journal.command("agent-a", "rerun", {}, "a", BASE)
    journal.command("agent-a", "settings", {"auto_apply": True}, "a", BASE)
    with pytest.raises(RequestConflict):
        journal.command("agent-a", "rerun", {}, "b", BASE)


@pytest.mark.parametrize(
    "state",
    [
        "HANDED_OFF",
        "PROBE_FAILED",
        "REJECTED",
        "UNRESOLVED",
        "ERROR",
        "CAS_CONFLICT",
        "RESTART_FAILED",
    ],
)
def test_can_rerun_for_states_a_person_should_retry(client, state):
    send_beat(client)
    assert put(client, run_body(state=state)).status_code == 204
    assert client.get(f"/api/v1/agent/runs/{RUN}").json()["can_rerun"] is True
    assert client.post(RERUN, headers=ORIGIN).status_code == 202
    assert client.get(f"/api/v1/agent/runs/{RUN}").json()["can_rerun"] is False


@pytest.mark.parametrize(
    "state", ["NOTHING_TO_FIX", "CHECKING", "WAITING_APPROVAL", "APPLYING", "RESOLVED"]
)
def test_cannot_rerun_from_other_states(client, state):
    send_beat(client)
    assert put(client, run_body(state=state)).status_code == 204
    assert client.get(f"/api/v1/agent/runs/{RUN}").json()["can_rerun"] is False


def test_rerun_pending_is_per_agent(client):
    send_beat(client)
    send_beat(client, agent_id="agent-b")
    assert put(client, run_body(state="ERROR")).status_code == 204
    assert client.post("/api/v1/agent/agents/agent-b/rerun", headers=ORIGIN).status_code == 202
    assert client.get(f"/api/v1/agent/runs/{RUN}").json()["can_rerun"] is True
    assert client.post(SETTINGS, json={"auto_apply": True}, headers=ORIGIN).status_code == 202
    assert client.get(f"/api/v1/agent/runs/{RUN}").json()["can_rerun"] is True


def test_login_mode_commands_need_deploy_role(env, store):
    app = create_app(registry=ProviderRegistry(), store=store, auth_mode="login")
    with TestClient(app, headers=ORIGIN) as anonymous:
        send_beat(anonymous)
        assert put(anonymous, run_body(state="ERROR")).status_code == 204
        assert anonymous.get("/api/v1/agent/status").status_code == 401
        assert anonymous.post(RERUN).status_code == 401
    clients = []
    for email in ("owner@example.com", "viewer@example.com"):
        signed = TestClient(app, headers=ORIGIN)
        signed.__enter__()
        clients.append(signed)
        body = {"email": email, "password": PASSWORD, "team": "platform"}
        assert signed.post("/api/v1/auth/signup", json=body).status_code == 201
    owner, viewer = clients
    try:
        assert viewer.get("/api/v1/agent/status").json()["can_command"] is False
        assert viewer.get(f"/api/v1/agent/runs/{RUN}").json()["can_rerun"] is False
        assert viewer.post(RERUN).status_code == 403
        assert viewer.post(SETTINGS, json={"auto_apply": True}).status_code == 403
        assert owner.get("/api/v1/agent/status").json()["can_command"] is True
        assert owner.get(f"/api/v1/agent/runs/{RUN}").json()["can_rerun"] is True
        assert owner.post(SETTINGS, json={"slack_enabled": False}).status_code == 202
        assert owner.post(RERUN).status_code == 202
        pending = owner.get("/api/v1/agent/status").json()["agents"][0]["pending_commands"]
        assert {c["requested_by"] for c in pending} == {"owner@example.com"}
    finally:
        for signed in clients:
            signed.__exit__(None, None, None)
