"""Real PostgreSQL journal (team ReleaseStore) + fake AWS: the AWS worker state machine."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from controlplane.domain.models import Runtime
from oneport_aws.candidates import CandidateGateway
from oneport_aws.execution.oneport_ecs_apps import AppRegistry
from oneport_aws.execution.oneport_ecs_runtime import OneportEcsSettings
from oneport_aws.execution.oneport_ecs_worker import OneportEcsWorker
from oneport_aws.providers.oneport_ecs import OneportEcsCandidateDriver, OneportEcsProvider

from .aws_fakes import error


@pytest.fixture
def harness(store, aws, environment, env_file, make_request):
    settings = OneportEcsSettings(
        env_file=env_file,
        application="shop",
        environment_id="aws-oneport",
        secret_refs=environment.secret_refs,
        database_url=store.dsn,
        required_health_samples=2,
        production_check_limit=3,
    )
    provider = OneportEcsProvider(aws, settings.environment())
    registry = AppRegistry(store.dsn)
    registry.migrate()
    registry.register(settings.environment(), port=8080, health_path="/health")
    runtime = SimpleNamespace(
        settings=settings, provider=provider, connect=Mock(), probe=Mock(return_value=True)
    )
    runtime.gateway = CandidateGateway(
        {("shop", "aws-oneport"): OneportEcsCandidateDriver(provider)}
    )
    return SimpleNamespace(
        store=store,
        aws=aws,
        runtime=runtime,
        worker=OneportEcsWorker(store, runtime),
        make_request=make_request,
        settings=settings,
    )


def enqueue(h, request, key):
    return h.store.enqueue(request, key, h.settings.fingerprint(), runtime=Runtime.AWS_ECS)


def run(h, release_id, until: set[str], limit=30):
    for _ in range(limit):
        with h.store.connect() as conn:  # make every deferred row due now; no sleeps in tests
            conn.execute("UPDATE releases SET next_attempt_at=now()")
        h.worker.tick()
        row = h.store.get(release_id)
        if row["state"] in until:
            return row
    raise AssertionError(f"stuck in {h.store.get(release_id)['state']}")


def test_first_release_succeeds_after_production_http(harness):
    h = harness
    row = enqueue(h, h.make_request(), "first-release-key-001")
    row = run(h, row["id"], {"waiting"})
    assert h.aws.services["shop"]["taskDefinition"].endswith("oneport-shop:1")
    h.aws.advance("shop", "IN_PROGRESS", "BAKE_TIME")  # ECS shifted traffic
    row = run(h, row["id"], {"succeeded", "failed", "uncertain"})
    assert row["state"] == "succeeded" and row["service_url"] == "https://shop.app.yubin.dev"
    states = [e["state"] for e in row["events"]]
    assert states[:3] == ["queued", "initializing", "initialized"] and "checking" in states


def test_ecs_rollback_during_deploy_keeps_previous(harness):
    h = harness
    first = enqueue(h, h.make_request(), "first-release-key-002")
    run(h, first["id"], {"waiting"})
    h.aws.advance("shop", "SUCCESSFUL")
    run(h, first["id"], {"succeeded"})

    second = enqueue(h, h.make_request(), "second-release-key-02")
    run(h, second["id"], {"waiting"})
    h.aws.advance("shop", "ROLLBACK_SUCCESSFUL")  # alarm/circuit breaker fired
    row = run(h, second["id"], {"failed", "uncertain"})
    assert row["state"] == "failed" and row["error_code"] == "ecs_rolled_back"
    assert h.runtime.provider.live_release() == first["id"]


def test_failed_production_check_rolls_back_inside_bake(harness):
    h = harness
    first = enqueue(h, h.make_request(), "first-release-key-003")
    run(h, first["id"], {"waiting"})
    h.aws.advance("shop", "SUCCESSFUL")
    run(h, first["id"], {"succeeded"})

    second = enqueue(h, h.make_request(), "second-release-key-03")
    run(h, second["id"], {"waiting"})
    h.aws.advance("shop", "IN_PROGRESS", "BAKE_TIME")
    h.runtime.probe.side_effect = lambda request, key: (
        key.value == first["id"]
    )  # new version broken
    run(h, second["id"], {"restore_checking"})
    assert "ecs.stop_service_deployment" in h.aws.calls
    h.aws.advance("shop", "ROLLBACK_SUCCESSFUL")
    row = run(h, second["id"], {"failed", "uncertain"})
    assert row["state"] == "failed" and row["error_code"] == "post_promotion_rolled_back"
    assert h.runtime.provider.live_release() == first["id"]


def test_bake_already_over_is_not_rolled_back_blindly(harness):
    h = harness
    row = enqueue(h, h.make_request(), "first-release-key-004")
    run(h, row["id"], {"waiting"})
    h.aws.advance("shop", "SUCCESSFUL")
    h.runtime.probe.return_value = False
    row = run(h, row["id"], {"failed", "uncertain"})
    assert row["state"] == "uncertain" and row["error_code"] == "rollback_window_closed"


def test_migration_is_accepted_at_enqueue_only_with_a_compatible_db_change(harness):
    h = harness
    migration = {"argv": ["alembic", "upgrade", "head"], "backward_compatible": True}
    h.settings.validate_request(
        h.make_request(resources={"database": "postgresql"}, migration=migration)
    )
    with pytest.raises(ValueError, match="DB"):
        h.make_request(migration=migration)  # a migration needs a database
    with pytest.raises(ValueError, match="호환"):
        h.make_request(
            resources={"database": "postgresql"},
            migration={**migration, "backward_compatible": False},
        )


def test_transient_aws_errors_defer_then_continue(harness):
    h = harness
    row = enqueue(h, h.make_request(), "first-release-key-005")
    h.aws.fail["elbv2.describe_rules"] = error("ThrottlingException")
    h.worker.tick()  # queued -> initializing, then throttled read
    assert h.store.get(row["id"])["state"] == "initializing"
    row = run(h, row["id"], {"waiting"})
    assert len(h.aws.services) == 1


def test_lost_submit_response_does_not_create_twice(harness):
    h = harness
    row = enqueue(h, h.make_request(), "first-release-key-006")
    run(h, row["id"], {"prepared"})
    from botocore.exceptions import ReadTimeoutError

    original = h.aws.ecs_create_service

    def created_then_lost(**kwargs):
        original(**kwargs)
        raise ReadTimeoutError(endpoint_url="https://ecs")

    h.aws.ecs_create_service = created_then_lost
    h.worker.tick()  # prepared -> submitting, response lost
    assert h.store.get(row["id"])["state"] == "submitting"
    h.aws.ecs_create_service = original
    row = run(h, row["id"], {"waiting"})
    assert len(h.aws.deployments["shop"]) == 1  # one deployment, no duplicate service


def test_changed_binding_is_never_executed(harness):
    h = harness
    row = enqueue(h, h.make_request(), "first-release-key-007")
    with h.store.connect() as conn:
        conn.execute("UPDATE releases SET binding_fingerprint='changed' WHERE id=%s", (row["id"],))
    h.worker.tick()
    assert h.store.get(row["id"])["state"] == "uncertain"
    assert not h.aws.services


def test_web_registered_priority_reconciles_external_rules(harness):
    h = harness
    # Another app on the shared ALB occupies the provisional web priority.
    other = {
        "RuleArn": "external-rule",
        "Priority": "100",
        "Conditions": [
            {"Field": "host-header", "HostHeaderConfig": {"Values": ["other.app.yubin.dev"]}}
        ],
        "Actions": [],
    }
    h.aws.rules.append(other)
    row = enqueue(h, h.make_request(), "priority-collision-test-001")
    run(h, row["id"], {"waiting"})
    assert h.runtime.provider.host_rule().priority == "101"
    assert AppRegistry(h.store.dsn).get("shop", "aws-oneport").rule_priority == 101
    assert other in h.aws.rules and other["Priority"] == "100"
