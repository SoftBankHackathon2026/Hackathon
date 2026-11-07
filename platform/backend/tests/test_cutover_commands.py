"""전환 제어 명령: 화면이 요청 → 러너 보고기가 claim → 결과 보고."""

from datetime import UTC, datetime, timedelta
from itertools import count

import pytest
from fastapi.testclient import TestClient

from controlplane.core.errors import InvalidRequest, RequestConflict
from controlplane.integrations.defaults import DEFAULT_FLEET
from controlplane.main import create_app
from controlplane.repositories.cutover_commands import CutoverCommandJournal, MemoryCutoverCommands
from controlplane.schemas.migrations import CommandCreate, CommandResult
from controlplane.services.cutover_commands import CutoverCommandService, status_for_exit

TOKEN = "ingest-test-token-0123456789-abcdef"
ORIGIN = {"Origin": "http://127.0.0.1:4320"}
BEARER = {"Authorization": "Bearer " + TOKEN}
NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


def ticking_clock():
    """호출마다 1초씩 흐르는 시계. 요청 시각이 같으면 최신순 정렬이 정해지지 않는다."""
    ticks = count()
    return lambda: NOW + timedelta(seconds=next(ticks))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("ONEPORT_CONFIG", raising=False)
    monkeypatch.delenv("ONEPORT_RUNTIME_CONFIG", raising=False)
    monkeypatch.delenv("ONEPORT_FLEET_CONFIG", raising=False)
    monkeypatch.setenv("ONEPORT_INGEST_TOKEN", TOKEN)
    with TestClient(create_app(auth_mode="demo")) as client:
        yield client


def test_exit_code_maps_to_command_status():
    assert status_for_exit(0) == "succeeded"
    assert status_for_exit(3) == "waiting_approval"
    assert status_for_exit(1) == "failed"
    assert status_for_exit(2) == "failed"


def test_service_requires_approval_id_and_rejects_concurrent_commands():
    service = CutoverCommandService(DEFAULT_FLEET, MemoryCutoverCommands(), now=ticking_clock())
    with pytest.raises(InvalidRequest):
        service.request("forward_001", CommandCreate(action="approve"), None)
    with pytest.raises(InvalidRequest):
        service.request("forward_001", CommandCreate(action="approve", approval_id="  "), None)
    first = service.request("forward_001", CommandCreate(action="run"), None)
    assert first.status == "pending" and first.requested_by is None
    with pytest.raises(RequestConflict):
        service.request("forward_001", CommandCreate(action="abort"), None)
    # 다른 작업은 막히지 않는다.
    other = service.request("return_001", CommandCreate(action="run"), None)
    assert other.job_id == "return_001"
    claimed = service.claim("forward_001")
    assert claimed.id == first.id and claimed.status == "running"
    with pytest.raises(RequestConflict):
        service.request("forward_001", CommandCreate(action="retry"), None)
    done = service.finish("forward_001", first.id, CommandResult(exit_code=3, stage="READY"))
    assert done.status == "waiting_approval"
    approved = service.request(
        "forward_001", CommandCreate(action="approve", approval_id="approve-forward-001"), None
    )
    assert approved.approval_id == "approve-forward-001"
    titles = [event.title for event in service.events("forward_001")]
    assert "명령 요청: 시작 (by 운영 화면)" in titles
    assert "명령 결과: 시작 승인 대기" in titles


def test_command_api_flow_in_simulation_mode(client):
    path = "/api/v1/migrations/forward_001/commands"
    # 화면 Origin 이 없는 POST 는 거절(기존 쓰기 경로와 같은 규칙).
    assert client.post(path, json={"action": "run"}).status_code == 403
    assert client.post(path, json={"action": "approve"}, headers=ORIGIN).status_code == 422
    assert client.post(path, json={"action": "explode"}, headers=ORIGIN).status_code == 422
    assert (
        client.post(
            "/api/v1/migrations/nope/commands", json={"action": "run"}, headers=ORIGIN
        ).status_code
        == 404
    )

    created = client.post(path, json={"action": "run", "note": "리허설"}, headers=ORIGIN)
    assert created.status_code == 202
    command = created.json()
    assert command["status"] == "pending" and command["action"] == "run"
    assert client.post(path, json={"action": "abort"}, headers=ORIGIN).status_code == 409
    assert [c["id"] for c in client.get(path).json()] == [command["id"]]

    claim = "/api/v1/ingest/migrations/forward_001/commands/claim"
    assert client.post(claim).status_code == 401
    claimed = client.post(claim, headers=BEARER)
    assert claimed.status_code == 200 and claimed.json()["status"] == "running"
    assert claimed.json()["started_at"] is not None
    assert client.post(claim, headers=BEARER).status_code == 204

    result = f"/api/v1/ingest/migrations/forward_001/commands/{command['id']}/result"
    body = {"exit_code": 3, "stage": "READY", "log_tail": "x" * 5000}
    assert client.post(result, json=body).status_code == 401
    finished = client.post(result, json=body, headers=BEARER)
    assert finished.status_code == 200
    assert finished.json()["status"] == "waiting_approval"
    assert len(finished.json()["log_tail"]) == 4000
    assert finished.json()["finished_at"] is not None
    # 이미 끝난 명령에 다시 결과를 올리면 409, 모르는 명령은 404.
    assert client.post(result, json=body, headers=BEARER).status_code == 409
    assert (
        client.post(
            "/api/v1/ingest/migrations/forward_001/commands/cmd_missing/result",
            json=body,
            headers=BEARER,
        ).status_code
        == 404
    )

    approve = client.post(
        path, json={"action": "approve", "approval_id": "approve-forward-001"}, headers=ORIGIN
    )
    assert approve.status_code == 202 and approve.json()["approval_id"] == "approve-forward-001"
    listed = client.get(path).json()
    assert [c["action"] for c in listed] == ["approve", "run"]
    events = client.get("/api/v1/migrations/forward_001/events").json()
    assert any(e["kind"] == "command" and e["title"].startswith("명령 요청: 승인") for e in events)


def test_journal_claims_oldest_pending_once_and_blocks_second_active(store):
    journal = CutoverCommandJournal(store)
    service = CutoverCommandService(DEFAULT_FLEET, journal, now=ticking_clock())
    first = service.request("forward_001", CommandCreate(action="run"), None)
    with pytest.raises(RequestConflict):
        service.request("forward_001", CommandCreate(action="run"), None)
    # 서비스 선점 검사를 우회해도 부분 유니크 인덱스가 두 번째 활성 명령을 막는다.
    with pytest.raises(RequestConflict):
        journal.create(first.model_copy(update={"id": "cmd_dup"}))
    assert journal.claim("return_001", NOW) is None
    claimed = journal.claim("forward_001", NOW)
    assert claimed.id == first.id and claimed.status == "running"
    assert journal.claim("forward_001", NOW) is None
    assert journal.active("forward_001").id == first.id
    finished = service.finish("forward_001", first.id, CommandResult(exit_code=1, log_tail="boom"))
    assert finished.status == "failed" and finished.exit_code == 1
    assert journal.active("forward_001") is None
    assert (
        journal.finish(
            "forward_001",
            first.id,
            status="failed",
            exit_code=1,
            stage=None,
            log_tail="",
            finished_at=NOW,
        )
        is None
    )
    second = service.request("forward_001", CommandCreate(action="retry"), None)
    assert [c.id for c in journal.list("forward_001")] == [second.id, first.id]


@pytest.mark.parametrize("make_store", ["memory", "journal"])
def test_claim_any_takes_the_oldest_pending_command_of_any_task(make_store, request):
    repository = (
        MemoryCutoverCommands()
        if make_store == "memory"
        else CutoverCommandJournal(request.getfixturevalue("store"))
    )
    service = CutoverCommandService(DEFAULT_FLEET, repository, now=ticking_clock())
    assert service.claim_any() is None
    older = service.request("return_001", CommandCreate(action="run"), None)
    newer = service.request("forward_001", CommandCreate(action="run"), None)
    first = service.claim_any()
    assert first.command.id == older.id and first.command.status == "running"
    assert (first.job_id, first.data_source, first.data_target) == (
        "return_001",
        "aws",
        "onprem_return",
    )
    assert (first.source_env, first.target_env) == ("aws", "onprem")
    assert (first.source_provider, first.target_provider) == ("aws", "onprem")
    assert service.claim_any().command.id == newer.id
    assert service.claim_any() is None


def test_claim_any_ignores_commands_of_unregistered_tasks():
    repository = MemoryCutoverCommands()
    stale = CutoverCommandService(
        DEFAULT_FLEET.model_copy(deep=True), repository, now=ticking_clock()
    )
    stale.request("forward_001", CommandCreate(action="run"), None)
    narrowed = DEFAULT_FLEET.model_copy(
        update={"migrations": [DEFAULT_FLEET.migration("return_001")]}
    )
    assert CutoverCommandService(narrowed, repository).claim_any() is None


def test_claim_any_endpoint_needs_the_ingest_token(client):
    path = "/api/v1/ingest/commands/claim"
    assert client.post(path).status_code == 401
    assert client.post(path, headers=BEARER).status_code == 204
    client.post("/api/v1/migrations/return_001/commands", json={"action": "run"}, headers=ORIGIN)
    claimed = client.post(path, headers=BEARER).json()
    assert claimed["job_id"] == "return_001" and claimed["command"]["action"] == "run"
    assert claimed["data_target"] == "onprem_return"
