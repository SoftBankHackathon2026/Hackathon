import pytest
from fastapi.testclient import TestClient
from test_agent_console import BASE, BEARER, ORIGIN, RUN, put, run_body, signed_in
from test_agent_console import client as client
from test_agent_console import env as env
from test_agent_console import login_app as login_app

from controlplane.main import create_app
from controlplane.providers.registry import ProviderRegistry
from controlplane.repositories.agent import AgentJournal
from controlplane.schemas.agent import RunReport

DELETE = "/api/v1/agent/runs/delete"
LIST = "/api/v1/agent/runs"
QUEUE = "/api/v1/ingest/agent/approvals"


def delete(client, *run_ids, headers=ORIGIN):
    return client.post(DELETE, json={"run_ids": list(run_ids)}, headers=headers)


def listed(client):
    return [run["run_id"] for run in client.get(LIST).json()["runs"]]


def test_delete_hides_runs_from_list_and_detail(client):
    for run_id in ("run-000001", "run-000002", "run-000003"):
        assert put(client, run_body(state="HANDED_OFF"), run_id=run_id).status_code == 204
    assert client.get(LIST).json()["can_delete"] is True
    assert all(run["can_delete"] for run in client.get(LIST).json()["runs"])
    assert client.get("/api/v1/agent/runs/run-000001").json()["can_delete"] is True

    response = delete(client, "run-000003", "run-000001")
    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": ["run-000003", "run-000001"], "skipped": []}
    assert listed(client) == ["run-000002"]
    assert client.get("/api/v1/agent/runs/run-000001").status_code == 404
    assert client.get("/api/v1/agent/runs/run-000003").status_code == 404
    assert client.get("/api/v1/agent/runs/run-000002").status_code == 200


def test_reported_again_a_deleted_run_stays_hidden(client):
    assert put(client, run_body(state="HANDED_OFF", updated=10)).status_code == 204
    assert delete(client, RUN).json()["deleted"] == [RUN]
    assert put(client, run_body(state="HANDED_OFF", updated=10)).status_code == 204
    assert put(client, run_body(state="RESOLVED", updated=60)).status_code == 204
    assert listed(client) == []
    assert client.get(f"/api/v1/agent/runs/{RUN}").status_code == 404
    assert delete(client, RUN).json() == {
        "deleted": [],
        "skipped": [{"run_id": RUN, "reason": "없는 실행"}],
    }


def test_hidden_run_keeps_the_latest_report(store):
    journal = AgentJournal(store)
    journal.put_run(RUN, RunReport.model_validate(run_body(state="HANDED_OFF", updated=10)))
    hidden, kept = journal.hide_runs([RUN], "운영 화면", BASE)
    assert hidden == {RUN} and kept == set()
    journal.put_run(RUN, RunReport.model_validate(run_body(state="RESOLVED", updated=60)))
    with journal.connect() as conn:
        row = conn.execute(
            "SELECT state, hidden_at, hidden_by FROM agent_runs WHERE run_id=%s", (RUN,)
        ).fetchone()
    assert row["state"] == "RESOLVED"
    assert row["hidden_at"] is not None and row["hidden_by"] == "운영 화면"


@pytest.mark.parametrize("state", ["CHECKING", "APPLYING", "WAITING_APPROVAL"])
def test_active_runs_are_skipped(client, state):
    assert put(client, run_body(state=state), run_id="run-0000aa").status_code == 204
    assert put(client, run_body(state="ERROR"), run_id="run-0000bb").status_code == 204
    rows = {run["run_id"]: run for run in client.get(LIST).json()["runs"]}
    assert rows["run-0000aa"]["can_delete"] is False and rows["run-0000bb"]["can_delete"] is True
    assert client.get("/api/v1/agent/runs/run-0000aa").json()["can_delete"] is False

    response = delete(client, "run-0000aa", "run-0000bb", "run-ffffff")
    assert response.status_code == 200
    assert response.json() == {
        "deleted": ["run-0000bb"],
        "skipped": [
            {"run_id": "run-0000aa", "reason": "진행 중인 실행"},
            {"run_id": "run-ffffff", "reason": "없는 실행"},
        ],
    }
    assert listed(client) == ["run-0000aa"]


def test_hidden_run_approval_is_not_served(client):
    for run_id in ("run-00000a", "run-00000b"):
        assert put(client, run_body(), run_id=run_id).status_code == 204
        approve = client.post(f"/api/v1/agent/runs/{run_id}/approve", headers=ORIGIN)
        assert approve.status_code == 202
    handed_off = run_body(state="HANDED_OFF", updated=20)
    assert put(client, handed_off, run_id="run-00000a").status_code == 204
    assert delete(client, "run-00000a").json()["deleted"] == ["run-00000a"]
    pending = client.get(QUEUE, params={"agent_id": "agent-a"}, headers=BEARER).json()
    assert [ticket["run_id"] for ticket in pending] == ["run-00000b"]
    assert client.post("/api/v1/agent/runs/run-00000a/approve", headers=ORIGIN).status_code == 404


def test_delete_needs_screen_origin(client):
    assert put(client, run_body(state="ERROR")).status_code == 204
    assert delete(client, RUN, headers={}).status_code == 403
    assert delete(client, RUN, headers={"Origin": "https://evil.example"}).status_code == 403
    assert delete(client, RUN, headers=BEARER).status_code == 403
    assert listed(client) == [RUN]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"run_ids": []},
        {"run_ids": "run-000001"},
        {"run_ids": ["run-XYZ"]},
        {"run_ids": ["job-0a1b2c3d"]},
        {"run_ids": ["run-000001", "run-000001"]},
        {"run_ids": [f"run-{index:06x}" for index in range(201)]},
        {"run_ids": ["run-000001"], "hard": True},
        [],
    ],
)
def test_delete_validation(client, body):
    assert client.post(DELETE, json=body, headers=ORIGIN).status_code == 422


def test_delete_accepts_two_hundred_ids(client):
    ids = [f"run-{index:06x}" for index in range(200)]
    response = client.post(DELETE, json={"run_ids": ids}, headers=ORIGIN)
    assert response.status_code == 200
    assert response.json()["deleted"] == [] and len(response.json()["skipped"]) == 200


def test_delete_needs_the_platform_database(env):
    with TestClient(create_app(registry=ProviderRegistry(), auth_mode="demo")) as bare:
        assert delete(bare, RUN).status_code == 503


def test_login_mode_requires_session_and_deploy_role(login_app, store):
    with TestClient(login_app, headers=ORIGIN) as anonymous:
        assert put(anonymous, run_body(state="ERROR")).status_code == 204
        assert delete(anonymous, RUN).status_code == 401
    owner = signed_in(login_app, "owner@example.com", "platform")
    viewer = signed_in(login_app, "viewer@example.com", "platform")
    try:
        assert viewer.get(LIST).json()["can_delete"] is False
        assert viewer.get(LIST).json()["runs"][0]["can_delete"] is False
        assert viewer.get(f"/api/v1/agent/runs/{RUN}").json()["can_delete"] is False
        assert delete(viewer, RUN).status_code == 403
        assert listed(owner) == [RUN]
        assert owner.get(f"/api/v1/agent/runs/{RUN}").json()["can_delete"] is True
        assert delete(owner, RUN).json()["deleted"] == [RUN]
        with store.connect() as conn:
            row = conn.execute("SELECT hidden_by FROM agent_runs WHERE run_id=%s", (RUN,))
            assert row.fetchone()["hidden_by"] == "owner@example.com"
    finally:
        owner.__exit__(None, None, None)
        viewer.__exit__(None, None, None)
