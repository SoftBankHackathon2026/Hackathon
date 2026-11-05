"""Real catalog/journal + SDK doubles: new cloud apps survive restarts and stay isolated."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aws.conftest import NOTION_ENV
from cloud_run_fakes import CloudRunApi
from google.api_core import exceptions as gexc
from google.cloud import run_v2

from controlplane.domain.models import Runtime, Strategy
from controlplane.execution.catalog_worker import CatalogAwsWorker, CatalogGcpWorker
from controlplane.execution.gcp.apps import GcpAppProvisioner
from controlplane.execution.gcp.runtime import GcpRuntime
from controlplane.execution.gcp.settings import GcpSettings
from controlplane.execution.gcp.worker import GcpWorker
from controlplane.providers.gcp.adapter import GcpAdapter
from controlplane.providers.gcp.models import GcpDeploymentRequest
from controlplane.providers.gcp.provider import CloudRunProvider
from controlplane.providers.registry import ProviderRegistry
from controlplane.schemas.catalog import CreateApplication
from oneport_aws.adapter import AwsAdapter
from oneport_aws.execution.oneport_ecs_runtime import OneportEcsRuntime, OneportEcsSettings

IMAGE = "asia-northeast3-docker.pkg.dev/test-project/apps/demo@sha256:" + "a" * 64


@pytest.fixture
def clouds(store, tmp_path):
    env_file = tmp_path / "aws.env"
    env_file.write_text(NOTION_ENV)
    registry = ProviderRegistry()
    registry.register(
        AwsAdapter(
            OneportEcsSettings(
                database_url=store.dsn,
                env_file=env_file,
                application="demo",
                environment_id="aws-existing",
                catalog_target_id="aws-main",
                allowed_images=[IMAGE],
            ),
            store=store,
        )
    )
    registry.register(
        GcpAdapter(
            GcpSettings(
                database_url=store.dsn,
                catalog_target_id="gcp-main",
                allowed_images=[IMAGE],
                environment=dict(
                    application="demo",
                    environment_id="gcp-existing",
                    project_id="test-project",
                    region="asia-northeast3",
                    service="existing",
                    service_uid="original-uid",
                    service_url="https://existing.run.app",
                ),
                required_health_samples=2,
                poll_seconds=1,
                production_check_limit=3,
                service_access="public",
            ),
            store=store,
        )
    )
    registry.enable_catalog(store)
    registry.initialize()
    return registry.catalog


def attach(catalog, target="gcp-main", name="a-very-long-application-name"):
    app = catalog.create(CreateApplication(name=name, idempotency_key="create-key-" + name))
    bound = catalog.attach(app["id"], target)
    assert catalog.attach(app["id"], target) == bound
    return catalog.resolve(bound["id"])


@pytest.mark.parametrize("target,limit", [("aws-main", 24), ("gcp-main", 19)])
def test_cloud_catalog_isolated_and_secret_free(clouds, target, limit):
    a, b = attach(clouds, target), attach(clouds, target, "second")
    assert a.application != b.application and len(a.application) <= limit
    assert a.settings.catalog_dynamic
    clouds.bootstrap()
    assert clouds.resolve(a.environment_id).fingerprint() == a.fingerprint()
    payload = json.dumps(clouds.binding(a.environment_id)["binding"])
    for forbidden in (
        clouds.store.dsn,
        "env_file",
        "credentials_file",
        "database_url",
        "fake-secret",
    ):
        assert forbidden not in payload
    assert len(clouds.store.list()) == 0
    if target == "gcp-main":
        assert a.settings.environment.service_uid is None
        assert a.settings.environment.service_url is None
        assert a.settings.catalog_owner != b.settings.catalog_owner


@pytest.mark.parametrize(
    "target,wrapper,runtime,patch",
    [
        (
            "aws-main",
            CatalogAwsWorker,
            OneportEcsRuntime,
            "oneport_aws.execution.oneport_ecs_worker.OneportEcsWorker.tick",
        ),
        (
            "gcp-main",
            CatalogGcpWorker,
            GcpRuntime,
            "controlplane.execution.gcp.worker.GcpWorker.tick",
        ),
    ],
)
def test_cloud_workers_discover_apps_even_after_creation_disabled(
    clouds, monkeypatch, target, wrapper, runtime, patch
):
    base = clouds.registry.get("aws-existing" if target == "aws-main" else "gcp-existing")
    tick = Mock()
    monkeypatch.setattr(patch, tick)
    worker = wrapper(clouds.store, runtime(base.settings))
    a, b = attach(clouds, target), attach(clouds, target, "second")
    base.settings.catalog_target_id = None
    clouds.bootstrap()
    worker.tick()
    assert set(worker.workers) == {a.environment_id, b.environment_id}
    assert tick.call_count == 3
    worker.close()


class NewCloudRunApi(CloudRunApi):
    def __init__(self, env):
        super().__init__(env.model_copy(update={"service_uid": "unused"}), IMAGE)
        self.missing = True
        self.creates = 0

    def get_service(self, **kwargs):
        if self.missing:
            raise gexc.NotFound("not created")
        return super().get_service(**kwargs)

    def create_service(self, *, parent, service_id, service, **kwargs):
        assert parent + "/services/" + service_id == self.env.service_name
        assert kwargs["retry"] is None and self.missing
        self.creates += 1
        self.missing = False
        self.service = run_v2.Service(service)
        self.service.name = self.env.service_name
        self.service.uid = "new-pinned-uid"
        self.service.uri = "https://new-app.run.app"
        self.service.etag = "etag-1"
        self.service.generation = 1
        # Server fills defaults that weren't present in the creation request.
        self.service.template.timeout = "300s"
        self.save_revision()
        self.settle()
        if self.fail_after_write:
            self.fail_after_write = False
            raise gexc.DeadlineExceeded("created, response lost")


class Workflow:
    def __init__(self, settings):
        self.settings, self.posts = settings, 0

    def request(self, method, url, **kwargs):
        env = self.settings.environment
        if method == "POST":
            assert json.loads(kwargs["json"]["argument"]) == {"app_id": env.application}
            self.posts += 1
            result = {"name": url.split("/v1/", 1)[1] + "/execution-1"}
        else:
            result = {
                "state": "SUCCEEDED",
                "result": json.dumps(
                    {
                        "schema_version": 1,
                        "app_id": env.application,
                        "runtime_service_account_email": f"oneport-rt-{env.application}@{env.project_id}.iam.gserviceaccount.com",
                    }
                ),
            }
        return SimpleNamespace(status_code=200, json=lambda: result)


def runtime_for(settings, api, workflow):
    rt = GcpRuntime(settings)
    rt.provider = CloudRunProvider(api, api, settings.environment)
    rt.connect = Mock()
    rt.probe = Mock(return_value=True)
    rt.app = GcpAppProvisioner(workflow, settings, rt.provider)
    return rt


def tick(store, worker):
    with store.connect() as conn:
        conn.execute("UPDATE releases SET next_attempt_at=now()")
    worker.tick()


def run(store, worker, row, states):
    for _ in range(40):
        actual = store.get(row["id"])
        if actual["state"] in states:
            return actual
        assert actual["state"] not in {"failed", "uncertain", "succeeded"}, (
            actual["state"],
            actual["error_code"],
        )
        tick(store, worker)
    pytest.fail("Worker did not finish")


@pytest.mark.parametrize("lost_response", [False, True])
def test_first_service_creation_restart_and_second_canary(clouds, lost_response):
    adapter = attach(clouds)
    settings, store = adapter.settings, clouds.store
    api, workflow = NewCloudRunApi(settings.environment), Workflow(settings)
    api.fail_after_write = lost_response
    rt = runtime_for(settings, api, workflow)
    worker = GcpWorker(store, rt)
    request = GcpDeploymentRequest.model_validate(adapter.public_environment()["request_template"])
    first = store.enqueue(
        request, "first-cloud-release-0001", settings.fingerprint(), runtime=Runtime.GCP_CLOUD_RUN
    )
    run(store, worker, first, {"submitting"})
    tick(store, worker)  # Create accepted; response can be lost.
    fresh = clouds.resolve(adapter.environment_id)
    assert fresh.fingerprint() == settings.fingerprint()
    rt = runtime_for(fresh.settings, api, workflow)
    worker = GcpWorker(store, rt)
    result = run(store, worker, first, {"succeeded"})
    assert result["service_url"] == "https://new-app.run.app"
    assert api.creates == workflow.posts == 1
    resolved = clouds.resolve(adapter.environment_id)
    assert resolved.settings.environment.service_uid == "new-pinned-uid"
    assert resolved.fingerprint() == settings.fingerprint()
    assert not any(e.name == "DB_PASSWORD" for e in api.service.template.containers[0].env)
    request.strategy = Strategy.CANARY
    second = store.enqueue(
        request, "second-cloud-release-0001", settings.fingerprint(), runtime=Runtime.GCP_CLOUD_RUN
    )
    row = run(store, worker, second, {"holding"})
    assert row["checkpoint"]["stable_release"] == first["id"]
    assert not row["checkpoint"].get("first_deployment")
    assert api.creates == 1
    store.request_action(
        second["id"],
        "rollback",
        application=request.application,
        environment_id=request.environment_id,
        binding_fingerprint=settings.fingerprint(),
    )
    restored = run(store, worker, second, {"failed"})
    assert restored["error_code"] == "operator_rollback_restored"
    from controlplane.providers.gcp.provider import weights

    assert weights(api.service) == {row["checkpoint"]["stable"]: 100}


def test_foreign_service_rejected_before_workflow_or_cloud_writes(clouds):
    adapter = attach(clouds)
    api, workflow = NewCloudRunApi(adapter.settings.environment), Workflow(adapter.settings)
    api.missing = False
    worker = GcpWorker(clouds.store, runtime_for(adapter.settings, api, workflow))
    request = GcpDeploymentRequest.model_validate(adapter.public_environment()["request_template"])
    row = clouds.store.enqueue(
        request, "foreign-service-test-001", adapter.fingerprint(), runtime=Runtime.GCP_CLOUD_RUN
    )
    result = run(clouds.store, worker, row, {"failed"})
    assert result["error_code"] == "gcp_app_preparation_failed"
    assert api.creates == workflow.posts == 0 and api.writes == []


def test_initial_health_failure_can_retry_without_duplicate_service(clouds):
    adapter = attach(clouds)
    api, workflow = NewCloudRunApi(adapter.settings.environment), Workflow(adapter.settings)
    rt = runtime_for(adapter.settings, api, workflow)
    rt.probe.return_value = False
    worker = GcpWorker(clouds.store, rt)
    request = GcpDeploymentRequest.model_validate(adapter.public_environment()["request_template"])
    first = clouds.store.enqueue(
        request, "unhealthy-initial-test-01", adapter.fingerprint(), runtime=Runtime.GCP_CLOUD_RUN
    )
    result = run(clouds.store, worker, first, {"failed"})
    assert result["error_code"] == "initial_service_unhealthy"
    rt.probe.return_value = True
    retry = clouds.store.enqueue(
        request, "retry-initial-service-001", adapter.fingerprint(), runtime=Runtime.GCP_CLOUD_RUN
    )
    run(clouds.store, worker, retry, {"succeeded"})
    assert api.creates == 1


@pytest.mark.parametrize("migration_result", [None, True, False])
def test_new_gcp_app_prepares_data_before_first_service(clouds, migration_result):
    from test_gcp_migration import MIGRATE, JobsApi

    from controlplane.execution.gcp.cache import GcpCacheProvisioner
    from controlplane.execution.gcp.data import GcpDataProvisioner
    from controlplane.execution.gcp.migration import GcpMigrationRunner
    from controlplane.execution.gcp.redis import GcpRedisConnector
    from controlplane.execution.gcp.settings import GcpCacheSettings, GcpDataSettings

    base = clouds.registry.get("gcp-existing").settings
    network = dict(
        app_id="demo",
        network="projects/test-project/global/networks/oneport",
        subnetwork="projects/test-project/regions/asia-northeast3/subnetworks/oneport",
    )
    base.data_provisioning = GcpDataSettings(**network, allocated_ip_range="private-services")
    base.cache_provisioning = GcpCacheSettings(**network)
    adapter = attach(clouds)
    settings = adapter.settings
    assert (
        settings.data_provisioning.app_id
        == settings.cache_provisioning.app_id
        == adapter.application
    )
    api, workflow = NewCloudRunApi(settings.environment), Workflow(settings)
    rt = runtime_for(settings, api, workflow)
    cache = GcpCacheProvisioner(Mock(), settings)
    data = GcpDataProvisioner(Mock(), settings)
    redis = GcpRedisConnector(Mock(), settings)
    jobs = JobsApi()
    rt.migrate = GcpMigrationRunner(jobs, settings)
    fingerprint = (cache.fingerprint, data.fingerprint, redis.fingerprint, rt.migrate.owner)
    data_binding = cache.binding() | {"secret": data.secret}
    rt.data = SimpleNamespace(step=Mock(return_value=("ready", data_binding)))
    rt.cache = SimpleNamespace(
        step=Mock(return_value=("ready", cache.binding())), binding=cache.binding
    )
    rt.redis = SimpleNamespace(
        step=Mock(return_value=("ready", redis.binding())), binding=redis.binding
    )
    worker = GcpWorker(clouds.store, rt)
    request = GcpDeploymentRequest.model_validate(adapter.public_environment()["request_template"])
    request.resources.database, request.resources.redis = "postgresql", True
    if migration_result is not None:
        request.migration = MIGRATE
        jobs.execution_succeeds = migration_result
        steps = [step.id for step in adapter.plan_steps(request)]
        assert steps.index("cache") < steps.index("migrate") < steps.index("deploy")
    row = clouds.store.enqueue(
        request, "new-gcp-with-data-0001", adapter.fingerprint(), runtime=Runtime.GCP_CLOUD_RUN
    )
    if migration_result is False:
        result = run(clouds.store, worker, row, {"failed"})
        assert result["error_code"] == "migration_failed"
        assert api.creates == 0 and api.writes == []
        return
    result = run(clouds.store, worker, row, {"submitting"})
    assert (
        api.creates == 0 and rt.data.step.called and rt.cache.step.called and rt.redis.step.called
    )
    if migration_result:
        assert result["checkpoint"]["migration_done"] and len(jobs.executions) == 1
    run(clouds.store, worker, row, {"succeeded"})
    secrets = {
        e.name: e.value_source.secret_key_ref.secret for e in api.service.template.containers[0].env
    }
    assert secrets["DATABASE_URL"] == data.secret and secrets["CACHE_CONFIG"] == cache.secret
    assert secrets["REDIS_URL"] == redis.secret_names["url"]
    assert api.service.template.volumes[0].secret.secret == redis.secret_names["ca"]
    resolved = clouds.resolve(adapter.environment_id).settings
    assert (
        GcpCacheProvisioner(Mock(), resolved).fingerprint,
        GcpDataProvisioner(Mock(), resolved).fingerprint,
        GcpRedisConnector(Mock(), resolved).fingerprint,
        GcpMigrationRunner(Mock(), resolved).owner,
    ) == fingerprint


def test_new_aws_app_reaches_existing_ecs_worker(clouds):
    from aws.aws_fakes import FakeAws

    from controlplane.domain.models import DeploymentRequest
    from oneport_aws.candidates import CandidateGateway
    from oneport_aws.execution.oneport_ecs_worker import OneportEcsWorker
    from oneport_aws.providers.oneport_ecs import OneportEcsCandidateDriver, OneportEcsProvider

    clouds.registry.get("aws-existing").settings.allowed_images = []
    adapter = attach(clouds, "aws-main")
    adapter.register_application(port=8080, health_path="/health")
    api = FakeAws()
    rt = OneportEcsRuntime(adapter.settings)
    rt.provider = OneportEcsProvider(api, adapter.environment)
    rt.driver = OneportEcsCandidateDriver(rt.provider)
    rt.gateway = CandidateGateway({(adapter.application, adapter.environment_id): rt.driver})
    rt.connect, rt.probe = Mock(), Mock(return_value=True)
    worker = OneportEcsWorker(clouds.store, rt)
    template = adapter.public_environment()["request_template"]
    template["source"]["image"] = (
        adapter.environment.repository_uri + "@" + api.push(adapter.environment.repository)
    )
    request = DeploymentRequest.model_validate(template)
    adapter.parse_request(request.model_dump(mode="json"))
    row = clouds.store.enqueue(
        request, "new-aws-first-release-01", adapter.fingerprint(), runtime=Runtime.AWS_ECS
    )
    run(clouds.store, worker, row, {"waiting"})
    assert set(api.services) == {adapter.application}
    api.advance(adapter.application, "IN_PROGRESS", "BAKE_TIME")
    result = run(clouds.store, worker, row, {"succeeded"})
    assert result["service_url"] == adapter.environment.url
