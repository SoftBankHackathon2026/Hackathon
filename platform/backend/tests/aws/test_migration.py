"""Release phase migration on AWS (infra data-access.md §3 step 9)."""

from unittest.mock import patch

import pytest

from controlplane.providers.contracts import ProviderRejected, ReleaseKey
from oneport_aws.adapter import AwsAdapter
from oneport_aws.providers.oneport_ecs import migration

from .aws_fakes import error
from .conftest import SECRET_REF
from .test_worker import enqueue, harness, run  # noqa: F401

MIGRATION = {"argv": ["python", "migrate.py"], "backward_compatible": True}


def db_app(h):
    h.settings.secret_refs = {"db": SECRET_REF}
    adapter = AwsAdapter(h.settings)
    adapter.initialize()
    adapter.register_application(port=8080, health_path="/health", database="postgresql")
    return adapter


def migrating_request(h, **fields):
    return h.make_request(resources={"database": "postgresql"}, migration=MIGRATION, **fields)


def test_migration_runs_before_ecs_and_then_deploys(harness):  # noqa: F811
    h = harness
    adapter = db_app(h)
    request = migrating_request(h)
    adapter.parse_request(request.model_dump(mode="json"))  # accepted at intake now
    row = enqueue(h, request, "migration-release-01")
    run(h, row["id"], {"waiting"})

    (task,) = h.aws.tasks.values()
    td = h.aws.task_definitions[task["taskDefinitionArn"]]
    assert td["family"] == "oneport-shop-migration"
    container = td["containerDefinitions"][0]
    assert container["name"] == "migration" and container["command"] == MIGRATION["argv"]
    assert "portMappings" not in container
    assert {"name": "DATABASE_URL", "valueFrom": SECRET_REF} in container["secrets"]
    assert task["network"]["awsvpcConfiguration"]["subnets"] == ["subnet-a", "subnet-c"]
    calls = h.aws.calls
    assert calls.index("ecs.run_task") < calls.index("ecs.create_service")

    h.aws.advance("shop", "SUCCESSFUL")
    done = run(h, row["id"], {"succeeded", "failed", "uncertain"})
    assert done["state"] == "succeeded"
    assert [e["state"] for e in done["events"]][3:6] == [
        "migration_preparing",
        "migration_submitting",
        "migration_waiting",
    ]


def test_failed_migration_stops_the_release_and_keeps_production(harness):  # noqa: F811
    h = harness
    db_app(h)
    h.aws.migration_exit = 1
    row = enqueue(h, migrating_request(h), "migration-release-02")
    done = run(h, row["id"], {"succeeded", "failed", "uncertain"})
    assert done["state"] == "failed" and done["error_code"] == "migration_failed"
    assert "exit code 1" in done["events"][-1]["message"]
    assert not any(c in h.aws.calls for c in ("ecs.create_service", "ecs.update_service"))
    families = {t["family"] for t in h.aws.task_definitions.values()}
    assert families == {"oneport-shop-migration"}  # the app revision was never registered


def test_lost_run_task_response_is_reconciled_not_repeated(harness):  # noqa: F811
    h = harness
    db_app(h)
    original = h.aws.ecs_run_task

    def started_but_lost(**kwargs):
        original(**kwargs)
        raise error("ServiceUnavailableException", "RunTask")

    row = enqueue(h, migrating_request(h), "migration-release-03")
    with patch.object(h.aws, "ecs_run_task", side_effect=started_but_lost):
        run(h, row["id"], {"migration_submitting"})
        h.worker.tick()  # RunTask "fails" after starting the task: deferred, not failed
    run(h, row["id"], {"waiting"})
    assert len(h.aws.tasks) == 1
    assert h.aws.calls.count("ecs.run_task") == 1


def test_only_the_migration_family_can_be_run(harness):  # noqa: F811
    h = harness
    provider = h.runtime.provider
    request = migrating_request(h)
    provider.initialize(request)
    h.settings.secret_refs = {"db": SECRET_REF}
    provider.environment = provider.environment.model_copy(
        update={"secret_refs": {"db": SECRET_REF}}
    )
    app_td = provider.register_task(request, ReleaseKey(value="4" * 24), delay=0)
    with pytest.raises(ProviderRejected, match="마이그레이션 family"):
        migration.run(provider, app_td, ReleaseKey(value="4" * 24))
    assert "ecs.run_task" not in h.aws.calls


def test_plan_lists_the_migration_step(harness):  # noqa: F811
    from controlplane.providers.interface import DeploymentSpec

    h = harness
    adapter = db_app(h)
    plan = adapter.plan(DeploymentSpec.model_validate(migrating_request(h).model_dump(mode="json")))
    assert plan.steps[0].id == "migrate"


def test_new_runtime_role_propagation_is_retried_not_failed(harness):  # noqa: F811
    from botocore.exceptions import ClientError

    h = harness
    db_app(h)
    h.aws.fail["ecs.run_task"] = ClientError(
        {
            "Error": {
                "Code": "ClientException",
                "Message": "ECS was unable to assume the role 'arn:aws:iam::1:role/oneport-rt-shop'",
            }
        },
        "RunTask",
    )
    row = enqueue(h, migrating_request(h), "migration-release-04")
    run(h, row["id"], {"waiting"})  # deferred once, then started and finished
    assert len(h.aws.tasks) == 1


def test_other_run_task_rejections_show_the_aws_reason(harness):  # noqa: F811
    from botocore.exceptions import ClientError

    h = harness
    db_app(h)
    h.aws.fail["ecs.run_task"] = ClientError(
        {"Error": {"Code": "ClientException", "Message": "No Container Instances were found"}},
        "RunTask",
    )
    row = enqueue(h, migrating_request(h), "migration-release-05")
    done = run(h, row["id"], {"failed", "uncertain"})
    assert done["error_code"] == "migration_rejected"
    assert "No Container Instances" in done["events"][-1]["message"]


def test_migration_task_with_tracing_still_ends_with_the_migration(harness):  # noqa: F811
    """X-Ray adds a collector sidecar; it must not keep the one-off task running."""
    h = harness
    db_app(h)
    provider = h.runtime.provider
    provider.environment = provider.environment.model_copy(
        update={
            "secret_refs": {"db": SECRET_REF},
            "tracing": "xray",
            "otel_collector_image": "public.ecr.aws/aws-observability/aws-otel-collector:latest",
        }
    )
    body = migration.payload(provider, migrating_request(h), ReleaseKey(value="5" * 24))
    first, *others = body["containerDefinitions"]
    assert first["name"] == "migration" and first["command"] == MIGRATION["argv"]
    assert others and all(c["essential"] is False for c in others)
