from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_agent_console import BEARER, signed_in
from test_agent_console import env as env

from controlplane.domain.fleet import TaskDefaults
from controlplane.domain.migration import MigrationStatus
from controlplane.execution.fleet_collector import FleetCollector
from controlplane.integrations.defaults import DEFAULT_FLEET
from controlplane.main import create_app
from controlplane.providers.registry import ProviderRegistry
from controlplane.repositories.cutover_commands import CutoverCommandJournal
from controlplane.repositories.migration_tasks import MemoryMigrationTasks, MigrationTaskJournal
from controlplane.schemas.migrations import (
    ConnectorView,
    CutoverCommand,
    MigrationCreate,
    TaskDeletion,
)
from controlplane.services.cutover_commands import CutoverCommandService
from controlplane.services.migration_tasks import MigrationTaskService
from controlplane.services.migrations import in_progress

ORIGIN = {"Origin": "http://127.0.0.1:4320"}
DELETE = "/api/v1/migrations/delete"
AT = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
# 시뮬레이션에서 온프렘 → AWS(정방향) 작업은 완료, AWS → 온프렘(되돌림) 작업은 승인 대기다.
FORWARD = {"source_env": "onprem", "target_env": "aws"}
RETURN = {"source_env": "aws", "target_env": "onprem"}
STATIC = DEFAULT_FLEET.migrations[0].job_id


def delete(client, *job_ids, headers=ORIGIN):
    return client.post(DELETE, json={"job_ids": list(job_ids)}, headers=headers)


def create(client, body=FORWARD):
    response = client.post("/api/v1/migrations", json=body, headers=ORIGIN)
    assert response.status_code == 201, response.text
    return response.json()["job_id"]


def fleet_rows(client):
    return {m["job_id"]: m for m in client.get("/api/v1/fleet").json()["migrations"]}


def task_paths(job_id):
    base = f"/api/v1/migrations/{job_id}"
    return [base, f"{base}/plan", f"{base}/series", f"{base}/events", f"{base}/commands"]


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.delenv("ONEPORT_FLEET_CONFIG", raising=False)
    monkeypatch.delenv("ONEPORT_ACCESS_PIN", raising=False)
    with TestClient(create_app(auth_mode="demo")) as client:
        yield client


@pytest.fixture
def db_client(env, store):
    app = create_app(registry=ProviderRegistry(), store=store, auth_mode="demo")
    with TestClient(app) as client:
        yield client


def test_fleet_marks_only_registered_tasks_as_deletable(demo):
    job_id = create(demo)
    fleet = demo.get("/api/v1/fleet").json()
    rows = {m["job_id"]: m for m in fleet["migrations"]}
    assert fleet["can_delete"] is True
    assert (rows[job_id]["registered"], rows[job_id]["can_delete"]) == (True, True)
    assert (rows[STATIC]["registered"], rows[STATIC]["can_delete"]) == (False, False)
    summary = demo.get(f"/api/v1/migrations/{job_id}").json()["summary"]
    assert (summary["registered"], summary["can_delete"]) == (True, True)
    static = demo.get(f"/api/v1/migrations/{STATIC}").json()["summary"]
    assert (static["registered"], static["can_delete"]) == (False, False)


def test_delete_removes_tasks_from_every_listing_and_detail(demo):
    first, second, kept = create(demo), create(demo), create(demo)
    response = delete(demo, second, first)
    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": [second, first], "skipped": []}
    rows = fleet_rows(demo)
    assert first not in rows and second not in rows and kept in rows
    costs = [m["job_id"] for m in demo.get("/api/v1/costs").json()["migrations"]]
    assert first not in costs and kept in costs
    for path in task_paths(first) + [f"/api/v1/migrations/{first}/data-mapping"]:
        assert demo.get(path).status_code == 404, path
    for path in task_paths(kept):
        assert demo.get(path).status_code == 200, path
    command = demo.post(
        f"/api/v1/migrations/{first}/commands", json={"action": "abort"}, headers=ORIGIN
    )
    assert command.status_code == 404


def test_running_tasks_are_refused(demo):
    waiting = create(demo, RETURN)
    done = create(demo)
    rows = fleet_rows(demo)
    assert rows[waiting]["status"] == "waiting_approval"
    assert (rows[waiting]["registered"], rows[waiting]["can_delete"]) == (True, False)
    assert demo.get(f"/api/v1/migrations/{waiting}").json()["summary"]["can_delete"] is False
    response = delete(demo, waiting, done)
    assert response.json() == {
        "deleted": [done],
        "skipped": [{"job_id": waiting, "reason": "진행 중인 작업"}],
    }
    assert waiting in fleet_rows(demo)


def test_a_task_with_a_waiting_command_is_refused(demo):
    job_id = create(demo)
    command = demo.post(
        f"/api/v1/migrations/{job_id}/commands", json={"action": "abort"}, headers=ORIGIN
    )
    assert command.status_code == 202, command.text
    assert fleet_rows(demo)[job_id]["can_delete"] is False
    assert demo.get(f"/api/v1/migrations/{job_id}").json()["summary"]["can_delete"] is False
    assert delete(demo, job_id).json()["skipped"] == [
        {"job_id": job_id, "reason": "진행 중인 작업"}
    ]
    assert job_id in fleet_rows(demo)


def test_static_and_unknown_tasks_are_skipped(demo):
    job_id = create(demo)
    response = delete(demo, STATIC, "onprem_to_aws_999", job_id)
    assert response.json() == {
        "deleted": [job_id],
        "skipped": [
            {"job_id": STATIC, "reason": "설정 파일 작업"},
            {"job_id": "onprem_to_aws_999", "reason": "없는 작업"},
        ],
    }
    assert STATIC in fleet_rows(demo)
    again = delete(demo, job_id).json()
    assert again == {"deleted": [], "skipped": [{"job_id": job_id, "reason": "없는 작업"}]}


def test_a_new_task_never_reuses_a_deleted_id(demo):
    first = create(demo)
    assert delete(demo, first).json()["deleted"] == [first]
    second = create(demo)
    assert second != first and second.endswith("_002")


def test_delete_needs_screen_origin(demo):
    job_id = create(demo)
    assert delete(demo, job_id, headers={}).status_code == 403
    assert delete(demo, job_id, headers={"Origin": "https://evil.example"}).status_code == 403
    assert job_id in fleet_rows(demo)


def test_operator_mode_needs_the_operator_token(monkeypatch):
    monkeypatch.delenv("ONEPORT_FLEET_CONFIG", raising=False)
    token = "t" * 40
    app = create_app(auth_mode="operator", operator_token=token, registry=ProviderRegistry())
    with TestClient(app) as client:
        bearer = {"Authorization": f"Bearer {token}"}
        job_id = client.post("/api/v1/migrations", json=FORWARD, headers=bearer).json()["job_id"]
        assert client.get("/api/v1/fleet").json()["can_delete"] is False
        assert client.get("/api/v1/fleet", headers=bearer).json()["can_delete"] is True
        assert delete(client, job_id, headers={}).status_code == 401
        assert delete(client, job_id, headers=BEARER).status_code == 401
        assert delete(client, job_id, headers=bearer).json()["deleted"] == [job_id]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"job_ids": []},
        {"job_ids": "onprem_to_aws_001"},
        {"job_ids": ["Onprem-to-aws"]},
        {"job_ids": ["1_task"]},
        {"job_ids": ["onprem_to_aws_001", "onprem_to_aws_001"]},
        {"job_ids": [f"task_{index:03d}" for index in range(201)]},
        {"job_ids": ["onprem_to_aws_001"], "hard": True},
        [],
    ],
)
def test_delete_validation(demo, body):
    assert demo.post(DELETE, json=body, headers=ORIGIN).status_code == 422


def test_delete_accepts_two_hundred_ids(demo):
    ids = [f"task_{index:03d}" for index in range(200)]
    response = demo.post(DELETE, json={"job_ids": ids}, headers=ORIGIN)
    assert response.status_code == 200
    assert response.json()["deleted"] == [] and len(response.json()["skipped"]) == 200


def test_journal_hides_the_row_and_records_who(db_client, store):
    job_id = create(db_client)
    assert delete(db_client, job_id).json()["deleted"] == [job_id]
    with store.connect() as conn:
        row = conn.execute(
            "SELECT hidden_at, hidden_by FROM fleet_migration_tasks WHERE job_id=%s", (job_id,)
        ).fetchone()
    assert row["hidden_at"] is not None and row["hidden_by"] == "운영 화면"
    assert job_id not in fleet_rows(db_client)
    assert db_client.get(f"/api/v1/migrations/{job_id}").status_code == 404
    assert create(db_client) != job_id


def test_restart_does_not_bring_a_deleted_task_back(env, store):
    app = create_app(registry=ProviderRegistry(), store=store, auth_mode="demo")
    with TestClient(app) as client:
        job_id = create(client)
        assert delete(client, job_id).json()["deleted"] == [job_id]
    restarted = create_app(registry=ProviderRegistry(), store=store, auth_mode="demo")
    with TestClient(restarted) as client:
        assert job_id not in fleet_rows(client)
        assert client.get(f"/api/v1/migrations/{job_id}").status_code == 404


def test_reports_for_a_deleted_task_are_refused(db_client):
    job_id = create(db_client)
    ingest = f"/api/v1/ingest/migrations/{job_id}"
    assert db_client.post(ingest, json={"policy_decisions": []}, headers=BEARER).status_code == 202
    assert delete(db_client, job_id).json()["deleted"] == [job_id]
    assert db_client.post(ingest, json={"policy_decisions": []}, headers=BEARER).status_code == 404
    claim = f"/api/v1/ingest/migrations/{job_id}/commands/claim"
    assert db_client.post(claim, headers=BEARER).status_code == 404
    assert job_id not in fleet_rows(db_client)


def test_journal_keeps_a_task_whose_command_is_still_active(store):
    tasks = MigrationTaskJournal(store)
    service = MigrationTaskService(DEFAULT_FLEET.model_copy(deep=True), tasks)
    job_id = service.create(MigrationCreate(**FORWARD), None).job_id
    CutoverCommandJournal(store).create(command(job_id, "running"))
    hidden, kept = tasks.hide([job_id, "onprem_to_aws_999"], "운영 화면", AT)
    assert (hidden, kept) == (set(), {job_id})
    assert [t.job_id for t in tasks.list()] == [job_id]


def test_sync_in_another_process_drops_a_deleted_task(store):
    journal = MigrationTaskJournal(store)
    api_config = DEFAULT_FLEET.model_copy(deep=True)
    api = MigrationTaskService(api_config, journal)
    job_id = api.create(MigrationCreate(**FORWARD), None).job_id
    collector_config = DEFAULT_FLEET.model_copy(deep=True)
    collector_config.task_defaults = TaskDefaults(worker_url="http://worker")
    collector = MigrationTaskService(collector_config, journal)
    collector.sync()
    assert collector_config.migration(job_id) is not None

    api.delete(TaskDeletion(job_ids=[job_id]), None, lambda ids: set())
    collector.sync()
    assert collector_config.migration(job_id) is None
    assert [m.job_id for m in collector_config.migrations] == [
        m.job_id for m in DEFAULT_FLEET.migrations
    ]
    polled = []
    fleet_collector = FleetCollector(collector_config, journal=None)
    fleet_collector.collect_worker = lambda job: polled.append(job.job_id)
    fleet_collector.collect_connectors = lambda job: None
    fleet_collector.collect_environment = lambda env: None
    fleet_collector.tick()
    assert job_id not in polled and polled == [m.job_id for m in DEFAULT_FLEET.migrations]


def test_runner_never_claims_a_command_of_a_deleted_task(store):
    journal = MigrationTaskJournal(store)
    other_config = DEFAULT_FLEET.model_copy(deep=True)
    other = MigrationTaskService(other_config, journal)
    job_id = other.create(MigrationCreate(**FORWARD), None).job_id
    assert journal.hide([job_id], "운영 화면", AT) == ({job_id}, set())
    commands = CutoverCommandJournal(store)
    commands.create(command(job_id, "pending"))
    # 이 프로세스는 아직 sync 전이라 작업을 들고 있다. 그래도 DB 가 숨긴 작업의 명령을 내주지 않는다.
    service = CutoverCommandService(other_config, commands)
    assert service.claim_any() is None
    assert commands.claim(job_id, AT) is None
    other.sync()
    assert other_config.migration(job_id) is None


def test_memory_store_records_who_deleted():
    repository = MemoryMigrationTasks()
    service = MigrationTaskService(DEFAULT_FLEET.model_copy(deep=True), repository)
    job_id = service.create(MigrationCreate(**FORWARD), None).job_id
    result = service.delete(TaskDeletion(job_ids=[job_id]), None, lambda ids: set())
    assert result.deleted == [job_id] and repository.hidden_by(job_id) == "운영 화면"
    assert repository.list() == [] and repository.used_ids() == {job_id}


def test_busy_check_only_sees_registered_candidates():
    asked = []
    service = MigrationTaskService(DEFAULT_FLEET.model_copy(deep=True), MemoryMigrationTasks())
    job_id = service.create(MigrationCreate(**FORWARD), None).job_id

    def busy(ids):
        asked.append(set(ids))
        return set(ids)

    result = service.delete(TaskDeletion(job_ids=[STATIC, job_id]), None, busy)
    assert asked == [{job_id}]
    assert [s.reason for s in result.skipped] == ["설정 파일 작업", "진행 중인 작업"]


def test_login_mode_requires_a_deploy_role_and_records_the_email(env, store):
    app = create_app(registry=ProviderRegistry(), store=store, auth_mode="login")
    with TestClient(app, headers=ORIGIN) as anonymous:
        assert delete(anonymous, "onprem_to_aws_001").status_code == 401
    owner = signed_in(app, "owner@example.com", "platform")
    viewer = signed_in(app, "viewer@example.com", "platform")
    try:
        job_id = create(owner)
        assert viewer.get("/api/v1/fleet").json()["can_delete"] is False
        assert fleet_rows(viewer)[job_id]["can_delete"] is False
        assert fleet_rows(viewer)[job_id]["registered"] is True
        assert delete(viewer, job_id).status_code == 403
        assert fleet_rows(owner)[job_id]["can_delete"] is True
        assert delete(owner, job_id).json()["deleted"] == [job_id]
        with store.connect() as conn:
            row = conn.execute(
                "SELECT hidden_by FROM fleet_migration_tasks WHERE job_id=%s", (job_id,)
            ).fetchone()
        assert row["hidden_by"] == "owner@example.com"
    finally:
        owner.__exit__(None, None, None)
        viewer.__exit__(None, None, None)


def command(job_id, status):
    return CutoverCommand(
        id=f"cmd_{status}",
        job_id=job_id,
        action="run",
        requested_by=None,
        requested_at=AT,
        status=status,
    )


def test_a_registered_task_with_other_data_names_can_be_listed_and_deleted(demo):
    job_id = create(demo, {"source_env": "onprem", "target_env": "gcp"})
    assert fleet_rows(demo)[job_id]["can_delete"] is True
    assert delete(demo, job_id).json()["deleted"] == [job_id]
    assert job_id not in fleet_rows(demo)


@pytest.mark.parametrize(
    "status, connector, busy",
    [
        ("pending", None, False),
        ("completed", None, False),
        ("aborted", "FAILED", False),
        ("rolled_back", None, False),
        ("running", None, True),
        ("waiting_approval", None, True),
        ("needs_recovery", None, True),
        ("completed", "RUNNING", True),
        ("aborted", "RUNNING", True),
    ],
)
def test_in_progress_follows_the_stage_and_the_cdc_connectors(status, connector, busy):
    connectors = (
        [
            ConnectorView(
                name="x-source", role="source", state=connector, tasks_running=1, tasks_total=1
            )
        ]
        if connector
        else []
    )
    assert in_progress(MigrationStatus(status), connectors) is busy
