import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from requests import ConnectionError
from test_gcp import advance, enqueue, tick
from test_gcp import harness as harness
from test_gcp import sdk as sdk
from test_gcp_data import data_harness  # noqa: F401, F811 -- fixture chain

from controlplane.domain.models import Migration
from controlplane.execution.gcp.migration import GcpMigrationRunner
from controlplane.execution.gcp.settings import GcpDataSettings
from controlplane.providers.gcp.adapter import GcpAdapter
from controlplane.providers.gcp.models import GcpDeploymentRequest

MIGRATE = Migration(
    argv=["sh", "scripts/start.sh", "migrate"], timeout_seconds=300, backward_compatible=True
)


@pytest.mark.parametrize("configured", [True, False])
def test_runtime_connects_migration_to_the_shared_data_session(sdk, monkeypatch, configured):
    from controlplane.execution.gcp import runtime as runtime_module

    if configured:
        sdk.settings.data_provisioning = GcpDataSettings(
            app_id="demo",
            network="projects/test-project/global/networks/oneport",
            subnetwork="projects/test-project/regions/asia-northeast3/subnetworks/oneport",
            allocated_ip_range="oneport-private-services",
        )
    session = Mock()
    credentials = Mock()
    monkeypatch.setattr(runtime_module, "gcp_credentials", Mock(return_value=credentials))
    authorized_session = Mock(return_value=session)
    monkeypatch.setattr(runtime_module, "AuthorizedSession", authorized_session)
    monkeypatch.setattr(runtime_module.run_v2, "ServicesClient", Mock())
    monkeypatch.setattr(runtime_module.run_v2, "RevisionsClient", Mock())
    monkeypatch.setattr(runtime_module.httpx, "Client", Mock())
    runtime = runtime_module.GcpRuntime(sdk.settings)
    runtime.connect()
    runtime.connect()  # connecting twice does not create another runner/session
    if configured:
        assert isinstance(runtime.migrate, GcpMigrationRunner)
        assert runtime.migrate.session is runtime.data.session is session
        authorized_session.assert_called_once_with(credentials)
    else:
        assert runtime.migrate is None and runtime.data is None
        authorized_session.assert_not_called()
    runtime.close()
    assert session.close.call_count == int(configured)


class JobsApi:
    """REST-shaped Cloud Run Jobs double: the executions list is the record of truth."""

    def __init__(self):
        self.job = None
        self.executions = []
        self.writes = []
        self.execution_succeeds = True
        self.execution_pending = False
        self.pending_operation = False
        self.lose_run_response = False
        self.lose_config_response = False
        self.hide_executions = False
        self.execution_vpc_omitted = False
        self.execution_image_mirror = False

    def _resp(self, body, status=200):
        return SimpleNamespace(status_code=status, json=lambda: deepcopy(body))

    def request(self, method, url, *, json=None, timeout=None):
        path = urlsplit(url).path
        if method == "GET":
            if path.endswith("/executions"):
                items = [] if self.hide_executions else deepcopy(self.executions)
                if items and self.execution_pending:
                    for key in ("conditions", "succeededCount", "failedCount"):
                        items[-1].pop(key, None)
                return self._resp({"executions": items})
            if "/operations/" in path:
                return self._resp({"done": not self.pending_operation})
            return self._resp(self.job, 200 if self.job else 404)
        if method == "POST" and path.endswith(":run"):
            self.writes.append(("run", deepcopy(json)))
            assert json == {"etag": self.job["etag"]}
            name = (
                self.job["name"]
                + f"/executions/oneport-migrate-demo-{len(self.executions) + 1:05d}"
            )
            execution = {
                "name": name,
                "generation": "1",  # immutable executions each start at generation 1
                "labels": deepcopy(self.job["template"]["labels"]),
                "template": deepcopy(self.job["template"]["template"]),
                "conditions": [
                    {
                        "type": "Completed",
                        "state": "CONDITION_SUCCEEDED"
                        if self.execution_succeeds
                        else "CONDITION_FAILED",
                    }
                ],
                "succeededCount": "1" if self.execution_succeeds else "0",
                "failedCount": "0" if self.execution_succeeds else "1",
            }
            if self.execution_vpc_omitted:
                execution["template"].pop("vpcAccess")
            if self.execution_image_mirror:
                container = execution["template"]["containers"][0]
                container["image"] = container["image"].replace("docker.io/", "mirror.gcr.io/", 1)
            self.executions.append(execution)
            if self.lose_run_response:
                self.lose_run_response = False
                raise ConnectionError("SENSITIVE transport payload")
            return self._resp({"name": "operations/run-" + name})
        if method == "POST" and path.endswith("/jobs"):
            assert "name" not in json  # real CreateJobRequest rejects Job.name
            self.writes.append(("create", deepcopy(json)))
            name = path.removeprefix("/v2/") + "/" + parse_qs(urlsplit(url).query)["jobId"][0]
            self.job = {**deepcopy(json), "name": name, "uid": "job-uid", "etag": "job-etag-1"}
            if self.lose_config_response:
                self.lose_config_response = False
                raise ConnectionError("SENSITIVE transport payload")
            return self._resp({"name": "operations/create-1"})
        if method == "PATCH":
            assert json["etag"] == self.job["etag"]
            assert json["name"] == self.job["name"]
            self.writes.append(("replace", deepcopy(json)))
            self.job = {**deepcopy(json), "uid": "job-uid", "etag": "job-etag-2"}
            return self._resp({"name": "operations/replace-1"})
        raise AssertionError(url)


@pytest.fixture
def migration_harness(data_harness):  # noqa: F811 -- fixture chain, resolved by pytest
    h = data_harness
    h.jobs_api = JobsApi()
    h.runtime.migrate = GcpMigrationRunner(h.jobs_api, h.settings)
    h.request = h.request.model_copy(update={"migration": MIGRATE})
    return h


def test_migration_requires_dedicated_postgres_and_rejects_custom_secrets(sdk):
    base = sdk.request.model_dump(mode="json")
    base["migration"] = MIGRATE.model_dump(mode="json")
    base["resources"].update(database="postgresql", database_mode="dedicated")
    assert GcpDeploymentRequest.model_validate(base).migration.argv == MIGRATE.argv

    no_database = deepcopy(base)
    no_database["resources"]["database"] = "none"
    with pytest.raises(ValueError, match="DB 요구사항"):
        GcpDeploymentRequest.model_validate(no_database)

    redis = deepcopy(base)
    redis["resources"]["redis"] = True
    assert GcpDeploymentRequest.model_validate(redis).resources.redis

    secrets = deepcopy(base)
    secrets["secret_bindings"] = [{"name": "EXTRA", "binding": "extra"}]
    with pytest.raises(ValueError, match="secrets"):
        GcpDeploymentRequest.model_validate(secrets)

    backward = deepcopy(base)
    backward["migration"]["backward_compatible"] = False
    with pytest.raises(ValueError, match="호환"):
        GcpDeploymentRequest.model_validate(backward)

    for mode in ("shared", "container"):
        unmanaged = deepcopy(base)
        unmanaged["resources"]["database_mode"] = mode
        with pytest.raises(ValueError, match="dedicated"):
            GcpDeploymentRequest.model_validate(unmanaged)


def test_migration_requires_registered_data_policy(sdk):
    base = sdk.request.model_dump(mode="json")
    base["migration"] = MIGRATE.model_dump(mode="json")
    base["resources"].update(database="postgresql", database_mode="dedicated")
    adapter = GcpAdapter(sdk.settings)
    assert "migration" in adapter.capabilities().configurable_fields
    assert adapter.capabilities().features["migration"].stage == "unsupported"
    with pytest.raises(ValueError, match="DB 생성 설정"):
        adapter.parse_request(base)
    assert not sdk.api.writes


def test_plan_shows_migration_between_data_and_deploy(migration_harness):
    h = migration_harness
    adapter = GcpAdapter(h.settings, store=h.store)
    assert adapter.capabilities().features["migration"].stage == "sdk_only"
    assert adapter.capabilities().features["redis"].stage == "unsupported"
    steps = [s.id for s in adapter.plan_steps(h.request)]
    assert steps == ["validate", "data", "migrate", "deploy", "verify", "promote", "bake"]
    assert not h.jobs_api.writes


def test_common_api_accepts_migration_without_cloud_writes(migration_harness):
    from fastapi.testclient import TestClient
    from test_gcp import HEADERS, TOKEN

    from controlplane.main import create_app
    from controlplane.providers.registry import ProviderRegistry

    h = migration_harness
    registry = ProviderRegistry()
    registry.register(GcpAdapter(h.settings, store=h.store))
    request = h.request.model_dump(mode="json")
    with TestClient(create_app(registry=registry, store=h.store, operator_token=TOKEN)) as client:
        plan = client.post("/api/v1/plans", json=request, headers=HEADERS)
        assert plan.status_code == 200
        assert [s["id"] for s in plan.json()["steps"]] == [
            "validate",
            "data",
            "migrate",
            "deploy",
            "verify",
            "promote",
            "bake",
        ]
        response = client.post(
            "/api/v1/releases",
            headers=HEADERS,
            json={
                "request": request,
                "idempotency_key": "gcp-api-migration-test",
            },
        )
        assert response.status_code == 202
    assert not h.jobs_api.writes and not h.api.writes


def test_migration_job_contract_uses_candidate_image_command_and_pinned_secret(
    migration_harness,
):
    h = migration_harness
    row = enqueue(h)
    advance(h, row, {"verifying"})
    mutations = [w for w in h.jobs_api.writes if w[0] in ("create", "replace")]
    assert len(mutations) == 1
    body = mutations[0][1]
    container = body["template"]["template"]["containers"][0]
    assert container["command"] == ["sh", "scripts/start.sh", "migrate"]
    assert container["image"] == h.request.source.image
    assert "value" not in container["env"][0]
    ref = container["env"][0]["valueSource"]["secretKeyRef"]
    assert ref["secret"] == "projects/test-project/secrets/oneport_demo_database"
    assert ref["version"] == "1"
    spec = body["template"]["template"]
    assert spec["serviceAccount"] == "oneport-rt-demo@test-project.iam.gserviceaccount.com"
    assert spec["vpcAccess"]["egress"] == "PRIVATE_RANGES_ONLY"
    assert spec["vpcAccess"]["networkInterfaces"][0]["network"].endswith("/oneport")
    assert spec["maxRetries"] == 0 and spec["timeout"] == "300s"
    assert body["labels"]["oneport-release"] == row["id"]
    assert body["template"]["labels"]["oneport-release"] == row["id"]
    saved = json.dumps(h.store.get(row["id"]), default=str)
    assert "postgresql://" not in saved


def test_migration_failure_blocks_candidate_deployment_and_keeps_live_app(
    migration_harness,
):
    h = migration_harness
    before = deepcopy(h.api.service)
    h.jobs_api.execution_succeeds = False
    row = enqueue(h)
    result = advance(h, row, {"failed"})
    assert result["error_code"] == "migration_failed"
    assert "배포는 시작하지 않았습니다" in result["events"][-1]["message"]
    assert not h.api.writes  # the registered service was never touched
    assert h.api.service == before


def test_lost_run_response_recovers_from_execution_list_without_rerun(migration_harness):
    h = migration_harness
    h.jobs_api.lose_run_response = True
    row = enqueue(h)
    advance(h, row, {"verifying"})
    runs = [w for w in h.jobs_api.writes if w[0] == "run"]
    assert len(runs) == 1


def test_config_response_loss_recovers_without_duplicate_write(migration_harness):
    h = migration_harness
    h.jobs_api.lose_config_response = True
    row = enqueue(h)
    advance(h, row, {"verifying"})
    assert [kind for kind, _ in h.jobs_api.writes] == ["create", "run"]


def test_create_job_omits_name_but_observes_fully_qualified_resource(migration_harness):
    h = migration_harness
    row = enqueue(h)
    result = advance(h, row, {"verifying"})
    assert "name" not in h.jobs_api.writes[0][1]
    name = h.jobs_api.job["name"]
    assert name.startswith("projects/test-project/locations/asia-northeast3/jobs/")
    assert result["checkpoint"]["migration_done"]["execution"].startswith(name + "/executions/")


def test_execution_accepts_omitted_vpc_with_strict_parent_job(migration_harness):
    h = migration_harness
    h.jobs_api.execution_vpc_omitted = True
    h.jobs_api.execution_image_mirror = True
    row = enqueue(h)
    result = advance(h, row, {"verifying"})
    assert result["checkpoint"]["migration_done"]
    assert [kind for kind, _ in h.jobs_api.writes] == ["create", "run"]


def test_execution_mirror_requires_the_identical_repository_and_pinned_digest():
    image = "docker.io/inhoman/demo@sha256:" + "a" * 64
    desired = {"containers": [{"image": image}]}
    observed = {"containers": [{"image": image.replace("docker.io/", "mirror.gcr.io/")}]}
    assert GcpMigrationRunner._execution_matches(observed, desired)
    assert desired["containers"][0]["image"] == image  # comparison does not mutate intent
    for wrong in (
        observed["containers"][0]["image"].replace("/inhoman/demo", "/other/demo"),
        observed["containers"][0]["image"].replace("a" * 64, "b" * 64),
        observed["containers"][0]["image"].replace("mirror.gcr.io", "other.example.com"),
    ):
        assert not GcpMigrationRunner._execution_matches(
            {"containers": [{"image": wrong}]}, desired
        )


@pytest.mark.parametrize("changed", ["image", "secret", "network"])
def test_execution_projection_still_rejects_image_secret_or_reported_vpc_drift(
    migration_harness, changed
):
    h = migration_harness
    row = enqueue(h)
    advance(h, row, {"migrating"})
    for _ in range(5):
        tick(h)
        if h.jobs_api.executions:
            break
    template = h.jobs_api.executions[0]["template"]
    if changed == "image":
        template["containers"][0]["image"] = "mirror.gcr.io/other/demo@sha256:" + "a" * 64
    elif changed == "secret":
        template["containers"][0]["env"][0]["valueSource"]["secretKeyRef"]["version"] = "99"
    else:
        template["vpcAccess"]["networkInterfaces"][0]["network"] += "-external"
    assert advance(h, row, {"uncertain"})["error_code"] == "ProviderConflict"
    assert not h.api.writes


def test_omitted_execution_vpc_does_not_hide_parent_job_network_drift(migration_harness):
    h = migration_harness
    h.jobs_api.execution_vpc_omitted = True
    row = enqueue(h)
    advance(h, row, {"migrating"})
    for _ in range(5):
        tick(h)
        if h.jobs_api.executions:
            break
    h.jobs_api.job["template"]["template"]["vpcAccess"]["networkInterfaces"][0]["network"] += (
        "-external"
    )
    assert advance(h, row, {"uncertain"})["error_code"] == "ProviderConflict"
    assert not h.api.writes


def test_job_operation_uses_done_boolean_and_waits(migration_harness):
    h = migration_harness
    h.jobs_api.pending_operation = True
    row = enqueue(h)
    advance(h, row, {"migrating"})
    tick(h)
    for _ in range(3):
        tick(h)
    assert [kind for kind, _ in h.jobs_api.writes] == ["create"]
    assert not h.api.writes
    h.jobs_api.pending_operation = False
    advance(h, row, {"verifying"})


def test_restart_after_lost_response_does_not_rerun_even_with_delayed_listing(migration_harness):
    from controlplane.execution.gcp.worker import GcpWorker

    h = migration_harness
    h.jobs_api.lose_run_response = True
    h.jobs_api.hide_executions = True
    row = enqueue(h)
    advance(h, row, {"migrating"})
    for _ in range(3):
        tick(h)
    assert h.store.get(row["id"])["checkpoint"]["migration"]["run_requested"] is True
    h.worker = GcpWorker(h.store, h.runtime)
    for _ in range(5):
        tick(h)
    assert [kind for kind, _ in h.jobs_api.writes].count("run") == 1
    h.jobs_api.hide_executions = False
    advance(h, row, {"verifying"})
    assert [kind for kind, _ in h.jobs_api.writes].count("run") == 1


def test_missing_execution_after_intent_becomes_uncertain_without_rerun(migration_harness):
    h = migration_harness
    h.jobs_api.hide_executions = True
    row = enqueue(h)
    result = advance(h, row, {"uncertain"})
    assert result["checkpoint"]["migration"]["run_requested"]
    assert not h.api.writes
    assert [kind for kind, _ in h.jobs_api.writes].count("run") == 1


def test_second_release_runs_its_own_migration_despite_same_execution_generation(migration_harness):
    h = migration_harness
    row = enqueue(h)
    advance(h, row, {"baking"})
    h.clock += 15
    advance(h, row, {"succeeded"})
    row2 = h.store.enqueue(
        h.request,
        "gcp-test-second-migration",
        h.settings.fingerprint(),
        runtime=h.worker.queue_runtime,
        timeout_seconds=h.settings.release_timeout_seconds,
    )
    advance(h, row2, {"verifying"})
    assert [kind for kind, _ in h.jobs_api.writes] == ["create", "run", "replace", "run"]
    assert {e["generation"] for e in h.jobs_api.executions} == {"1"}
    done = h.store.get(row2["id"])["checkpoint"]["migration_done"]
    assert done["execution"] == h.jobs_api.executions[1]["name"]


def test_unowned_job_cannot_be_taken_over(migration_harness):
    h = migration_harness
    h.jobs_api.job = {"labels": {"managed-by": "other"}}
    row = enqueue(h)
    assert advance(h, row, {"uncertain"})["error_code"] == "ProviderConflict"
    assert not h.jobs_api.writes and not h.api.writes


def test_ready_condition_alone_is_not_completed_migration():
    assert (
        GcpMigrationRunner._state(
            {"conditions": [{"type": "Ready", "state": "CONDITION_SUCCEEDED"}]}
        )
        == "running"
    )
    assert GcpMigrationRunner._state({"succeededCount": 1, "failedCount": 1}) == "failed"


def test_cloud_run_omitted_scalar_defaults_match_requested_zero_retries():
    assert GcpMigrationRunner._matches({"timeout": "300s"}, {"maxRetries": 0, "timeout": "300s"})
    assert not GcpMigrationRunner._matches({"maxRetries": 3}, {"maxRetries": 0})


def test_running_execution_is_never_run_concurrently(migration_harness):
    h = migration_harness
    h.jobs_api.execution_pending = True
    row = enqueue(h)
    with h.store.worker_lock() as conn:
        for _ in range(6):
            h.worker.tick()
            # direct stepping needs the release row again
            conn.execute("UPDATE releases SET next_attempt_at=now()")
    h.jobs_api.execution_pending = False
    advance(h, row, {"verifying"})
    assert [w[0] for w in h.jobs_api.writes].count("run") == 1


def test_unhealthy_candidate_rolls_back_and_keeps_migration_and_binding(
    migration_harness,
):
    h = migration_harness
    row = enqueue(h)
    advance(h, row, {"baking"})
    # Stable-release probes succeed; every probe of this release's candidate fails.
    h.runtime.probe.side_effect = lambda request, service, *, url, release: release != row["id"]
    h.clock += 15
    result = advance(h, row, {"failed"})
    assert result["error_code"] == "health_check_failed_restored"
    assert h.store.get(row["id"])["checkpoint"]["migration_done"]["job"] == ("oneport-migrate-demo")
    assert len(h.jobs_api.executions) == 1
    service = h.api.service
    assert service.template.containers[0].image == h.request.source.image
    database_url = next(e for e in service.template.containers[0].env if e.name == "DATABASE_URL")
    assert database_url.value_source.secret_key_ref.version == "1"
    traffic = {t.revision.rsplit("/", 1)[-1]: t.percent for t in service.traffic}
    assert max(traffic.values()) == 100


def test_worker_reaches_candidate_only_after_migration_success(migration_harness):
    h = migration_harness
    row = enqueue(h)
    advance(h, row, {"data_preparing"})
    tick(h)
    assert h.store.get(row["id"])["state"] in {"data_preparing", "queued"}
    assert not h.jobs_api.writes or all(
        w[0] != "run" for w in h.jobs_api.writes
    )  # data first, migration second
    h.jobs_api.execution_pending = True
    advance(h, row, {"migrating"})
    for _ in range(6):
        tick(h)
    assert h.jobs_api.executions
    assert not h.api.writes
    assert h.store.get(row["id"])["state"] == "migrating"
    h.jobs_api.execution_pending = False
    completed = advance(h, row, {"queued"})
    assert completed["checkpoint"]["migration_done"]
    assert not h.api.writes
    advance(h, row, {"verifying"})
    assert [w[0] for w in h.jobs_api.writes if w[0] == "run"] == ["run"]


def test_migration_deadline_blocks_candidate_and_preserves_stable_traffic(migration_harness):
    h = migration_harness
    h.jobs_api.execution_pending = True
    row = enqueue(h)
    advance(h, row, {"migrating"})
    for _ in range(6):
        tick(h)
    before = deepcopy(h.api.service)
    with h.store.connect() as conn:
        conn.execute("UPDATE releases SET deadline=now()-interval '1 second'")
    result = advance(h, row, {"failed"})
    assert result["error_code"] == "deadline_exceeded"
    assert not h.api.writes and h.api.service == before
    assert [kind for kind, _ in h.jobs_api.writes].count("run") == 1


def test_unconfigured_runner_blocks_candidate(migration_harness):
    h = migration_harness
    h.runtime.migrate = None
    row = enqueue(h)
    result = advance(h, row, {"failed"})
    assert result["error_code"] == "migration_unconfigured"
    assert not h.api.writes and not h.jobs_api.writes
