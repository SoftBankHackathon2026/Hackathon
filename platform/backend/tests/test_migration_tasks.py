import pytest
from fastapi.testclient import TestClient

from controlplane.core.errors import InvalidRequest, OperationNotFound, RequestConflict
from controlplane.domain.migration import Direction
from controlplane.integrations.defaults import DEFAULT_FLEET
from controlplane.main import create_app
from controlplane.repositories.migration_tasks import MemoryMigrationTasks, MigrationTaskJournal
from controlplane.schemas.migrations import MigrationCreate
from controlplane.services.migration_tasks import MigrationTaskService
from controlplane.services.migrations import MigrationService

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


def test_data_names_can_follow_the_runner_targets():
    config, tasks = service()
    create(
        tasks,
        source_env="onprem",
        target_env="aws",
        data_source="onprem_return",
        data_target="aws",
    )
    task = config.migration("onprem_to_aws_001")
    assert (task.data_source, task.data_target) == ("onprem_return", "aws")


NOT_STARTED = lambda job_id: False  # noqa: E731
STARTED = lambda job_id: True  # noqa: E731


def test_a_created_task_that_never_started_can_be_removed():
    repository = MemoryMigrationTasks()
    config, tasks = service(repository)
    create(tasks, source_env="onprem", target_env="aws")
    assert tasks.removable("onprem_to_aws_001", NOT_STARTED)
    tasks.remove("onprem_to_aws_001", NOT_STARTED)
    assert config.migration("onprem_to_aws_001") is None
    assert repository.list() == []
    assert create(tasks, source_env="onprem", target_env="aws").job_id == "onprem_to_aws_001"


@pytest.mark.parametrize(
    "job_id, started, error",
    [
        ("forward_001", NOT_STARTED, InvalidRequest),
        ("onprem_to_aws_001", STARTED, RequestConflict),
        ("missing_001", NOT_STARTED, OperationNotFound),
    ],
)
def test_fixed_started_or_unknown_tasks_are_kept(job_id, started, error):
    config, tasks = service()
    create(tasks, source_env="onprem", target_env="aws")
    with pytest.raises(error):
        tasks.remove(job_id, started)
    assert not tasks.removable(job_id, started)
    assert config.migration("forward_001") is not None
    assert config.migration("onprem_to_aws_001") is not None


def test_sync_drops_tasks_removed_by_another_process():
    repository = MemoryMigrationTasks()
    config, collector = service(repository)
    _, api = service(repository)
    create(api, source_env="onprem", target_env="aws")
    collector.sync()
    assert config.migration("onprem_to_aws_001") is not None
    repository.remove("onprem_to_aws_001")
    collector.sync()
    assert config.migration("onprem_to_aws_001") is None
    assert config.migration("forward_001") is not None


def test_journal_remove_clears_the_task_and_its_records(store):
    journal = MigrationTaskJournal(store)
    _, tasks = service(journal)
    create(tasks, source_env="onprem", target_env="aws")
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO fleet_data_mappings(job_id, inventory_hash, bindings, confirmed_at) "
            "VALUES('onprem_to_aws_001','h','[]',now())"
        )
    assert journal.remove("onprem_to_aws_001") is True
    assert journal.list() == []
    with store.connect() as conn:
        left = conn.execute(
            "SELECT count(*) AS n FROM fleet_data_mappings WHERE job_id='onprem_to_aws_001'"
        ).fetchone()
    assert left["n"] == 0
    assert journal.remove("onprem_to_aws_001") is False


def test_delete_endpoint_removes_a_task_and_its_plan_says_so(monkeypatch):
    monkeypatch.delenv("ONEPORT_ACCESS_PIN", raising=False)
    monkeypatch.setattr(MigrationService, "has_started", lambda self, job_id: False)
    client = TestClient(create_app(auth_mode="demo"), headers=ORIGIN)
    job_id = client.post(
        "/api/v1/migrations", json={"source_env": "onprem", "target_env": "aws"}
    ).json()["job_id"]
    assert client.get(f"/api/v1/migrations/{job_id}/plan").json()["removable"] is True
    assert client.get("/api/v1/migrations/forward_001/plan").json()["removable"] is False
    assert client.delete("/api/v1/migrations/forward_001").status_code == 422
    assert client.delete(f"/api/v1/migrations/{job_id}").status_code == 204
    assert client.get(f"/api/v1/migrations/{job_id}").status_code == 404
