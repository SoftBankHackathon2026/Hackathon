"""App deletion from the console: the AWS worker removes the app one step per tick."""

import pytest

from controlplane.core.errors import RequestConflict
from controlplane.domain.models import Runtime
from controlplane.repositories.deletions import DeletionStore
from oneport_aws.execution.oneport_ecs_apps import AppRegistry

from .test_dedicated_resources import DEDICATED, register, tick_until_ready
from .test_worker import enqueue, harness, run  # noqa: F401


def deploy(h, request, key):
    row = enqueue(h, request, key)
    run(h, row["id"], {"waiting"})
    h.aws.advance("shop", "IN_PROGRESS", "BAKE_TIME")
    row = run(h, row["id"], {"succeeded", "failed", "uncertain"})
    assert row["state"] == "succeeded"
    return row


def request_deletion(h, key, *, delete_data=False):
    return DeletionStore(h.store.dsn).request(
        application="shop",
        environment_id="aws-oneport",
        runtime=Runtime.AWS_ECS,
        delete_data=delete_data,
        key=key,
    )


def run_deletion(h, limit=40):
    deletions = DeletionStore(h.store.dsn)
    for _ in range(limit):
        with h.store.connect() as conn:  # every deferred step is due now; no sleeps
            conn.execute("UPDATE app_deletions SET next_attempt_at=now()")
        h.worker.tick()
        row = deletions.latest("aws-oneport")
        if row["state"] in {"deleted", "failed"}:
            return row
    raise AssertionError(f"stuck in {row['state']}")


def test_delete_keeps_data_and_registration_then_redeploys(harness):  # noqa: F811
    h = harness
    deploy(h, h.make_request(), "before-delete-key-0001")
    assert h.aws.rules[-1]["Conditions"]  # host rule exists

    request_deletion(h, "delete-keep-data-0001")
    row = run_deletion(h)

    assert row["state"] == "deleted", row["events"]
    assert h.aws.services["shop"]["status"] == "INACTIVE"
    assert not h.aws.target_groups and not h.aws.alarms and not h.aws.roles
    assert all(td["status"] == "INACTIVE" for td in h.aws.task_definitions.values())
    assert [e["state"] for e in row["events"]][:3] == [
        "requested",
        "service_draining",
        "routing",
    ]
    # History is archived, the registration is kept: the next deploy starts clean.
    assert h.store.list() == []
    assert AppRegistry(h.store.dsn).get("shop", "aws-oneport") is not None
    deploy(h, h.make_request(), "after-delete-key-00001")
    assert h.aws.services["shop"]["status"] == "ACTIVE"


def test_delete_with_data_removes_rds_cache_and_registration(harness):  # noqa: F811
    h = harness
    register(h, **DEDICATED)
    tick_until_ready(h)
    deploy(h, h.make_request(resources=DEDICATED), "dedicated-before-delete1")

    request_deletion(h, "delete-with-data-00001", delete_data=True)
    row = run_deletion(h)

    assert row["state"] == "deleted", row["events"]
    assert h.aws.db is None and h.aws.deleted_db["final_snapshot"].startswith(
        "oneport-db-shop-final-"
    )
    assert not h.aws.caches and not h.aws.cache_groups and not h.aws.cache_users
    assert AppRegistry(h.store.dsn).get("shop", "aws-oneport") is None
    assert "최종 스냅샷" in row["events"][-1]["message"]
    assert h.aws.calls.count("rds.delete_db_instance") == 1


def test_deploys_and_deletions_never_overlap(harness):  # noqa: F811
    h = harness
    active = enqueue(h, h.make_request(), "active-release-key-001")
    with pytest.raises(RequestConflict, match="배포"):
        request_deletion(h, "blocked-delete-key-001")
    run(h, active["id"], {"waiting"})
    h.aws.advance("shop", "IN_PROGRESS", "BAKE_TIME")
    run(h, active["id"], {"succeeded"})

    request_deletion(h, "delete-in-progress-001")
    with pytest.raises(RequestConflict, match="삭제"):
        enqueue(h, h.make_request(), "deploy-while-delete-01")
    with pytest.raises(RequestConflict, match="이미"):
        request_deletion(h, "second-delete-key-0001")
    # The same request key is idempotent.
    assert request_deletion(h, "delete-in-progress-001")["state"] == "requested"
    assert run_deletion(h)["state"] == "deleted"


def test_lost_rds_delete_response_is_not_repeated(harness):  # noqa: F811
    from unittest.mock import patch

    from .aws_fakes import error

    h = harness
    register(h, database="postgresql", database_mode="dedicated")
    tick_until_ready(h)
    request_deletion(h, "lost-rds-delete-key-01", delete_data=True)
    original = h.aws.rds_delete_db_instance

    def accepted_but_lost(**kwargs):
        original(**kwargs)
        raise error("ServiceUnavailable")

    with patch.object(h.aws, "rds_delete_db_instance", side_effect=accepted_but_lost):
        row = run_deletion(h)
    assert row["state"] == "deleted", row["events"]
    assert h.aws.calls.count("rds.delete_db_instance") == 1


def test_api_checks_the_typed_name_and_shows_progress(harness):  # noqa: F811
    from fastapi.testclient import TestClient

    from controlplane.main import create_app
    from controlplane.providers.registry import ProviderRegistry
    from oneport_aws.adapter import AwsAdapter

    h = harness
    registry = ProviderRegistry()
    registry.register(AwsAdapter(h.settings))
    token = "deletion-test-operator-token-0123456789"
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(registry=registry, store=h.store, operator_token=token)) as client:
        url = "/api/v1/environments/aws-oneport/deletion"
        wrong = client.post(
            url,
            json={"confirm": "shop2", "idempotency_key": "wrong-name-key-000001"},
            headers=headers,
        )
        assert wrong.status_code == 409 and "이름" in wrong.json()["detail"]
        assert not h.aws.calls  # nothing in AWS from HTTP

        env = next(e for e in client.get("/api/v1/environments").json() if e["id"] == "aws-oneport")
        assert env["app_deletion"]["supported"] and env["app_deletion"]["recreatable"]
        assert env["deletion"] is None

        accepted = client.post(
            url,
            json={"confirm": "shop", "idempotency_key": "right-name-key-000001"},
            headers=headers,
        )
        assert accepted.status_code == 202 and accepted.json()["status"] == "running"
        assert not h.aws.calls

        run_deletion(h)
        latest = client.get(url, headers=headers).json()
        assert latest["status"] == "deleted" and latest["events"][-1]["phase"] == "deleted"
        env = next(e for e in client.get("/api/v1/environments").json() if e["id"] == "aws-oneport")
        assert env["deletion"]["status"] == "deleted"
