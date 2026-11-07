import pytest
from fastapi.testclient import TestClient

from controlplane.core.errors import InvalidRequest
from controlplane.domain.migration import Direction
from controlplane.integrations.defaults import DEFAULT_FLEET
from controlplane.main import create_app
from controlplane.repositories.migration_tasks import MemoryMigrationTasks, MigrationTaskJournal
from controlplane.schemas.migrations import MigrationCreate
from controlplane.services.migration_tasks import MigrationTaskService

ORIGIN = {"Origin": "http://127.0.0.1:4320"}


def service(repository=None):
    config = DEFAULT_FLEET.model_copy(deep=True)
    return config, MigrationTaskService(config, repository or MemoryMigrationTasks())


def create(tasks, **body):
    return tasks.create(MigrationCreate(**body), "owner@example.com")


def test_a_new_task_gets_a_route_id_and_joins_the_fleet():
    config, tasks = service()
    created = create(
        tasks,
        source_env="aws",
        target_env="connected-1",
        target_provider="onprem",
        apps=["sample-app"],
    )
    assert (created.job_id, created.label) == ("aws_to_onprem_001", "AWS → 온프렘")
    task = config.migration("aws_to_onprem_001")
    assert (task.source_provider, task.target_provider) == ("aws", "onprem")
    assert task.direction == Direction.RETURN
    assert task.apps == ["sample-app"]
    assert (task.data_source, task.data_target) == ("aws", "connected-1")


def test_ids_count_up_per_route_and_direction_follows_the_target():
    config, tasks = service()
    create(tasks, source_env="aws", target_env="x", target_provider="onprem")
    second = create(tasks, source_env="aws", target_env="y", target_provider="onprem")
    forward = create(tasks, source_env="onprem", target_env="gcp")
    assert second.job_id == "aws_to_onprem_002"
    assert forward.job_id == "onprem_to_gcp_001"
    assert config.migration("onprem_to_gcp_001").direction == Direction.FORWARD


def test_known_environments_use_their_own_provider():
    config, tasks = service()
    created = create(tasks, source_env="aws", target_env="gcp", source_provider="onprem")
    assert created.job_id == "aws_to_gcp_001"


@pytest.mark.parametrize(
    "body, message",
    [
        ({"source_env": "aws", "target_env": "aws"}, "같아요"),
        ({"source_env": "aws", "target_env": "unknown"}, "환경 종류"),
        ({"source_env": "unknown", "target_env": "aws"}, "환경 종류"),
    ],
)
def test_invalid_requests_are_rejected(body, message):
    config, tasks = service()
    with pytest.raises(InvalidRequest, match=message):
        create(tasks, **body)
    assert len(config.migrations) == len(DEFAULT_FLEET.migrations)


def test_sync_loads_saved_tasks_once():
    repository = MemoryMigrationTasks()
    _, first = service(repository)
    create(first, source_env="aws", target_env="x", target_provider="onprem")
    config, second = service(repository)
    second.sync()
    second.sync()
    assert [t.job_id for t in config.migrations].count("aws_to_onprem_001") == 1
    assert create(second, source_env="aws", target_env="z", target_provider="onprem").job_id == (
        "aws_to_onprem_002"
    )


def test_journal_keeps_tasks_in_creation_order(store):
    journal = MigrationTaskJournal(store)
    _, tasks = service(journal)
    create(tasks, source_env="aws", target_env="x", target_provider="onprem", apps=["a"])
    create(tasks, source_env="onprem", target_env="gcp")
    saved = journal.list()
    assert [t.job_id for t in saved] == ["aws_to_onprem_001", "onprem_to_gcp_001"]
    assert saved[0].apps == ["a"] and saved[0].target_provider == "onprem"


def test_create_endpoint_makes_a_task_with_its_plan(monkeypatch):
    monkeypatch.delenv("ONEPORT_ACCESS_PIN", raising=False)
    client = TestClient(create_app(auth_mode="demo"), headers=ORIGIN)
    created = client.post(
        "/api/v1/migrations",
        json={"source_env": "aws", "target_env": "connected-1", "target_provider": "onprem"},
    )
    assert created.status_code == 201
    job_id = created.json()["job_id"]
    plan = client.get(f"/api/v1/migrations/{job_id}/plan").json()
    assert plan["route"]["label"] == "AWS → 온프렘"
    assert client.get(f"/api/v1/migrations/{job_id}").status_code == 200
    assert job_id in [m["job_id"] for m in client.get("/api/v1/fleet").json()["migrations"]]
    rejected = client.post("/api/v1/migrations", json={"source_env": "aws", "target_env": "aws"})
    assert rejected.status_code == 422
    assert "같아요" in rejected.json()["detail"]


def test_a_task_saved_elsewhere_is_not_given_the_same_id():
    repository = MemoryMigrationTasks()
    _, other_process = service(repository)
    create(other_process, source_env="aws", target_env="x", target_provider="onprem")
    _, this_process = service(repository)
    created = create(this_process, source_env="aws", target_env="y", target_provider="onprem")
    assert created.job_id == "aws_to_onprem_002"


def test_creating_a_task_does_not_change_the_built_in_fleet(monkeypatch):
    monkeypatch.delenv("ONEPORT_FLEET_CONFIG", raising=False)
    monkeypatch.delenv("ONEPORT_ACCESS_PIN", raising=False)
    before = [m.job_id for m in DEFAULT_FLEET.migrations]
    client = TestClient(create_app(auth_mode="demo"), headers=ORIGIN)
    client.post(
        "/api/v1/migrations",
        json={"source_env": "aws", "target_env": "z", "target_provider": "onprem"},
    )
    assert [m.job_id for m in DEFAULT_FLEET.migrations] == before

