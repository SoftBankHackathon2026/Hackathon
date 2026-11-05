"""Opt-in per-app resources chosen at app registration (Notion §3 step 3, data-access.md §4):
dedicated RDS PostgreSQL and ElastiCache Serverless (Valkey), provisioned by the worker."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from controlplane.core.errors import RequestConflict, UnsupportedOperation
from controlplane.domain.models import Runtime
from controlplane.providers.capabilities import provider_capabilities
from controlplane.providers.durable import DurableDeploymentProvider
from controlplane.providers.interface import DeploymentSpec
from oneport_aws.adapter import AwsAdapter
from oneport_aws.execution.oneport_ecs_apps import AppRegistry
from oneport_aws.execution.oneport_ecs_db import provision_step
from oneport_aws.providers.oneport_ecs import cache
from oneport_aws.providers.oneport_ecs.models import environment_from_env_file

from .aws_fakes import error
from .test_worker import enqueue, harness, run  # noqa: F401

DEDICATED = {"database": "postgresql", "database_mode": "dedicated", "redis": True}


def register(h, **choices):
    h.settings.db_settle_seconds = 0
    h.settings.cache_settle_seconds = 0
    adapter = AwsAdapter(h.settings)
    adapter.initialize()
    result = adapter.register_application(port=8080, health_path="/health", **choices)
    return adapter, result["application_registration"]


def tick_until_ready(h, limit=20):
    registry = AppRegistry(h.store.dsn)
    for _ in range(limit):
        h.worker.tick()
        app = registry.get("shop", "aws-oneport")
        if app.db_state in {"ready", "none"} and app.cache_state in {"ready", "none"}:
            return app
    raise AssertionError(f"stuck: db={app['db_state']} cache={app['cache_state']}")


def test_web_registration_records_choices_without_calling_aws(harness):  # noqa: F811
    from fastapi.testclient import TestClient

    from controlplane.main import create_app
    from controlplane.providers.registry import ProviderRegistry

    h = harness
    registry = ProviderRegistry()
    registry.register(AwsAdapter(h.settings))
    token = "dedicated-test-operator-token-0123456789"
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(registry=registry, store=h.store, operator_token=token)) as client:
        response = client.post(
            "/api/v1/environments/aws-oneport/application",
            json={"port": 8080, "health_path": "/health", **DEDICATED},
            headers=headers,
        )
    assert response.status_code == 200
    body = response.json()
    registration = body["application_registration"]
    assert registration["database_mode"] == "dedicated"
    assert registration["db_state"] == "requested" and registration["cache_state"] == "requested"
    assert body["request_template"]["resources"] == DEDICATED
    assert not h.aws.calls  # HTTP never waits for or starts cloud work


def test_worker_provisions_db_and_cache_then_injects_both(harness):  # noqa: F811
    h = harness
    adapter, _ = register(h, **DEDICATED)
    request = h.make_request(resources=DEDICATED)
    with pytest.raises(RequestConflict, match="준비"):
        adapter.parse_request(request.model_dump(mode="json"))

    early = enqueue(h, request, "dedicated-too-early-01")
    h.worker.tick()  # one DB step, one cache step, then the release: not ready yet
    assert h.store.get(early["id"])["error_code"] in {"db_not_ready", "cache_not_ready"}

    app = tick_until_ready(h)
    assert (app.db_state, app.cache_state) == ("ready", "ready")
    refs = AppRegistry(h.store.dsn).refs("shop", "aws-oneport")
    assert set(refs) == {"db", "cache"}

    name = "oneport-cache-shop"
    created = h.aws.caches[name]
    assert created["UserGroupId"] == name and created["SecurityGroupIds"] == ["sg-data"]
    assert h.aws.cache_groups[name]["UserIds"] == [name, f"{name}-d"]
    assert h.aws.cache_users[f"{name}-d"]["AccessString"] == "off -@all"
    url = list(h.aws.secret_store["oneport/shop/cache"]["versions"].values())[-1]
    password = h.aws.cache_users[name]["password"]
    assert url == f"rediss://shop:{password}@{created['Endpoint']['Address']}:6379"
    assert "secrets.get_secret_value" not in h.aws.calls
    adapter.parse_request(request.model_dump(mode="json"))  # now accepted

    row = enqueue(h, request, "dedicated-release-key-1")
    run(h, row["id"], {"waiting"})
    h.aws.advance("shop", "SUCCESSFUL")
    assert run(h, row["id"], {"succeeded", "failed", "uncertain"})["state"] == "succeeded"
    td = h.aws.task_definitions[h.aws.services["shop"]["taskDefinition"]]
    assert sorted(
        (s["name"], s["valueFrom"]) for s in td["containerDefinitions"][0]["secrets"]
    ) == [("DATABASE_URL", refs["db"]), ("REDIS_URL", refs["cache"])]


def test_request_must_match_registered_resources(harness):  # noqa: F811
    h = harness
    adapter, _ = register(h, **DEDICATED)
    tick_until_ready(h)
    for resources in (
        {"database": "postgresql", "database_mode": "shared", "redis": True},
        {"database": "postgresql", "database_mode": "dedicated", "redis": False},
    ):
        with pytest.raises(ValueError, match="registered"):
            adapter.parse_request(h.make_request(resources=resources).model_dump(mode="json"))


def test_lost_cache_create_response_is_not_replayed(harness):  # noqa: F811
    h = harness
    register(h, redis=True)
    registry = AppRegistry(h.store.dsn)
    provider = h.runtime.provider
    original = h.aws.elasticache_create_serverless_cache

    def accepted_but_lost(**kwargs):
        original(**kwargs)
        raise error("ServiceUnavailableException")

    for _ in range(2):  # users + group, then one wait while the group is still creating
        provision_step(registry, provider, settle_seconds=0, kind="cache")
    with patch.object(h.aws, "elasticache_create_serverless_cache", side_effect=accepted_but_lost):
        step = provision_step(registry, provider, settle_seconds=0, kind="cache")
    assert step.state == "uncertain"
    assert provision_step(registry, provider, kind="cache").state == "uncertain"
    assert h.aws.calls.count("elasticache.create_serverless_cache") == 1
    assert "elasticache.modify_user" not in h.aws.calls


def test_cache_stays_until_explicitly_deleted_in_aws_order(harness):  # noqa: F811
    h = harness
    adapter, _ = register(h, redis=True)
    tick_until_ready(h)
    with pytest.raises(ValueError, match="cache-delete"):
        adapter.register_application(port=8080, health_path="/health", database="none")
    h.runtime.provider.delete_app()
    assert "oneport-cache-shop" in h.aws.caches  # deleting the app keeps data

    for _ in range(10):
        finished, _ = cache.delete_step(h.runtime.provider)
        if finished:
            break
    assert finished and not h.aws.caches and not h.aws.cache_groups and not h.aws.cache_users
    order = [c for c in h.aws.calls if c.startswith("elasticache.delete_")]
    assert order[0] == "elasticache.delete_serverless_cache"
    assert order[1] == "elasticache.delete_user_group"
    AppRegistry(h.store.dsn).reset_cache("shop", "aws-oneport")
    result = adapter.register_application(port=8080, health_path="/health", database="none")
    assert result["application_registration"]["cache_state"] == "none"


def test_cache_names_fit_elasticache_and_infra_limits(env_file):
    short = environment_from_env_file(env_file, application="shop", environment_id="e")
    assert short.cache_name == "oneport-cache-shop"
    long = environment_from_env_file(env_file, application="a" * 30, environment_id="e")
    assert long.cache_name.startswith("oneport-cache-") and len(long.cache_name) <= 38


def test_long_app_ids_cannot_choose_a_cache(harness):  # noqa: F811
    h = harness
    h.settings.application = "a" * 25
    adapter = AwsAdapter(h.settings)
    adapter.initialize()
    with pytest.raises(ValueError, match="24자"):
        adapter.register_application(port=8080, health_path="/health", database="none", redis=True)


def test_providers_without_dedicated_reject_it_before_intake():
    class Onprem(DurableDeploymentProvider):
        runtime = Runtime.ONPREM_ARGO
        application, environment_id = "shop", "onprem"

        def capabilities(self):
            return provider_capabilities(self.runtime)

        def parse_request(self, payload):
            return payload

        def fingerprint(self):
            return "f"

        def plan_steps(self, request):
            return []

        def public_environment(self):
            return {}

    adapter = Onprem(SimpleNamespace(database_url=SecretStr("x")), store=SimpleNamespace(dsn="x"))
    spec = {
        "application": "shop",
        "environment_id": "onprem",
        "source": {"kind": "image", "image": "r/a@sha256:" + "0" * 64},
    }
    adapter.validate(
        DeploymentSpec.model_validate({**spec, "resources": {"database": "postgresql"}})
    )
    with pytest.raises(UnsupportedOperation, match="DB 방식"):
        adapter.validate(
            DeploymentSpec.model_validate(
                {**spec, "resources": {"database": "postgresql", "database_mode": "dedicated"}}
            )
        )
