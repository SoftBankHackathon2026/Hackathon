import base64
import json as jsonlib
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient
from google.cloud import run_v2
from requests import ConnectionError
from test_gcp import HEADERS, TOKEN, advance, enqueue, tick
from test_gcp import harness as harness
from test_gcp import sdk as sdk
from test_gcp_data import data_harness  # noqa: F401, F811 -- fixture chain
from test_gcp_migration import migration_harness  # noqa: F401, F811 -- fixture chain

from controlplane.core.errors import RequestConflict
from controlplane.domain.models import ResourceRequirements, Runtime
from controlplane.execution.gcp.cache import GcpCacheProvisioner
from controlplane.execution.gcp.data import DataProvisionError
from controlplane.execution.gcp.redis import (
    REDIS_CA_DIR,
    REDIS_CA_FILE,
    REDIS_CA_VOLUME,
    GcpRedisConnector,
    redis_status,
)
from controlplane.execution.gcp.settings import GcpCacheSettings
from controlplane.main import create_app
from controlplane.providers.contracts import ProviderConflict, ProviderUnavailable
from controlplane.providers.gcp.adapter import GcpAdapter
from controlplane.providers.gcp.models import GcpDeploymentRequest
from controlplane.providers.gcp.provider import weights
from controlplane.providers.registry import ProviderRegistry
from controlplane.repositories.deletions import DeletionStore

AUTH = "sensitive-password:/@?#&"
CA = "-----BEGIN CERTIFICATE-----\nTEST-CA\n-----END CERTIFICATE-----\n"


class RedisApi:
    """REST-shaped workflow, Memorystore and Secret Manager fake."""

    def __init__(self, settings):
        self.env = settings.environment
        self.policy = settings.cache_provisioning
        self.instance = None
        self.executions = []
        self.secrets = {}
        self.versions = {}
        self.writes = []
        self.pending = False
        self.failed = False
        self.fail_status = None
        self.lose_workflow_response = False
        self.lose_secret_response = None
        self.hide_executions = False

    def ready_instance(self):
        return {
            "name": f"projects/{self.env.project_id}/locations/{self.env.region}/instances/oneport-cache-demo",
            "labels": {"managed_by": "oneport-cache-workflow", "app_id": "demo"},
            "authorizedNetwork": self.policy.network,
            "connectMode": "PRIVATE_SERVICE_ACCESS",
            "tier": "BASIC",
            "memorySizeGb": 1,
            "redisVersion": "REDIS_7_2",
            "authEnabled": True,
            "transitEncryptionMode": "SERVER_AUTHENTICATION",
            "state": "READY",
            "host": "10.30.0.8",
            "port": 6378,
            "serverCaCerts": [{"cert": CA}],
            "createTime": "2026-10-04T00:00:00Z",
        }

    @staticmethod
    def response(body, status=200):
        return SimpleNamespace(status_code=status, json=lambda: deepcopy(body))

    def execution(self, saved):
        result = deepcopy(saved)
        result["state"] = "ACTIVE" if self.pending else "FAILED" if self.failed else "SUCCEEDED"
        result["error"] = {"payload": "SENSITIVE cloud error"}
        return result

    def request(self, method, url, *, json=None, timeout=None):
        path = urlsplit(url).path.removeprefix("/v1/")
        if self.fail_status:
            return self.response({"error": "SENSITIVE response payload"}, self.fail_status)
        if method == "GET":
            if path.endswith("/executions"):
                assert parse_qs(urlsplit(url).query)["view"] == ["FULL"]
                return self.response(
                    {
                        "executions": []
                        if self.hide_executions
                        else [self.execution(e) for e in self.executions]
                    }
                )
            if "/executions/" in path:
                found = next((e for e in self.executions if e["name"] == path), None)
                return (
                    self.response(self.execution(found), 200) if found else self.response({}, 404)
                )
            if path.endswith("/authString"):
                return self.response({"authString": AUTH})
            if "/instances/" in path:
                return self.response(self.instance, 200 if self.instance else 404)
            if "/versions/" in path:
                return self.response({"name": path, "state": "ENABLED"})
            if path.endswith("/versions"):
                name = path.removesuffix("/versions")
                return self.response(
                    {
                        "versions": [
                            {"name": name + "/versions/" + v, "state": "ENABLED"}
                            for v in self.versions.get(name, {})
                        ]
                    }
                )
            return self.response(self.secrets.get(path), 200 if path in self.secrets else 404)
        assert method == "POST"
        self.writes.append((path, deepcopy(json)))
        if path.endswith("/executions"):
            assert json["callLogLevel"] == "LOG_NONE"
            assert jsonlib.loads(json["argument"]) == {
                "action": "create",
                "app_id": "demo",
            }
            self.instance = self.ready_instance()
            execution = {
                "name": path + "/execution-1",
                "argument": json["argument"],
                "labels": json.get("labels", {}),
                "result": jsonlib.dumps(
                    {
                        "schema_version": 1,
                        "action": "create",
                        "app_id": "demo",
                        "instance": self.instance["name"],
                        "state": "READY",
                    }
                ),
            }
            self.executions.append(execution)
            if self.lose_workflow_response:
                self.lose_workflow_response = False
                raise ConnectionError("SENSITIVE transport payload")
            return self.response(execution)
        if path.endswith(":addVersion"):
            name = path.removesuffix(":addVersion")
            versions = self.versions.setdefault(name, {})
            number = str(len(versions) + 1)
            versions[number] = base64.b64decode(json["payload"]["data"]).decode()
            if self.lose_secret_response and name.endswith(self.lose_secret_response):
                self.lose_secret_response = None
                raise ConnectionError("SENSITIVE transport payload")
            return self.response({"name": name + "/versions/" + number})
        assert path.endswith("/secrets")
        name = path + "/" + parse_qs(urlsplit(url).query)["secretId"][0]
        self.secrets[name] = json
        return self.response({"name": name})


def enable_connection(h):
    policy = h.settings.data_provisioning
    h.settings.cache_provisioning = GcpCacheSettings(
        app_id=policy.app_id, network=policy.network, subnetwork=policy.subnetwork
    )
    h.redis_api = RedisApi(h.settings)
    h.runtime.cache = GcpCacheProvisioner(h.redis_api, h.settings)
    h.runtime.redis = GcpRedisConnector(h.redis_api, h.settings)


@pytest.fixture
def redis_harness(data_harness):  # noqa: F811 -- fixture chain
    h = data_harness
    enable_connection(h)
    h.runtime.redis = GcpRedisConnector(h.redis_api, h.settings)
    h.request = h.request.model_copy(
        update={
            "resources": ResourceRequirements(
                database="none", database_mode="dedicated", redis=True
            )
        }
    )
    return h


def prepare_redis(h, limit=30):
    with h.store.worker_lock() as conn:
        for _ in range(limit):
            _, cache = h.runtime.cache.step(conn)
            if not cache:
                continue
            phase, binding = h.runtime.redis.step(conn)
            if binding:
                return binding
    pytest.fail("Redis preparation did not complete")


def test_redis_requires_registered_policy_but_does_not_require_postgres(sdk):
    payload = sdk.request.model_dump(mode="json")
    payload["resources"]["redis"] = True
    parsed = GcpDeploymentRequest.model_validate(payload)
    assert parsed.resources.database == "none" and parsed.resources.redis
    with pytest.raises(ValueError, match="캐시 생성 설정"):
        sdk.settings.validate_request(parsed)
    payload["secret_bindings"] = [{"name": "EXTRA", "binding": "extra"}]
    with pytest.raises(ValueError, match="custom secrets"):
        GcpDeploymentRequest.model_validate(payload)


def test_cache_only_runtime_shares_redis_session_and_closes_it_once(sdk, monkeypatch):
    from unittest.mock import Mock

    from controlplane.execution.gcp import runtime as runtime_module

    sdk.settings.cache_provisioning = GcpCacheSettings(
        app_id="demo",
        network="projects/test-project/global/networks/oneport",
        subnetwork="projects/test-project/regions/asia-northeast3/subnetworks/oneport",
    )
    session = Mock()
    monkeypatch.setattr(runtime_module, "gcp_credentials", Mock())
    sessions = Mock(return_value=session)
    monkeypatch.setattr(runtime_module, "AuthorizedSession", sessions)
    monkeypatch.setattr(runtime_module.run_v2, "ServicesClient", Mock())
    monkeypatch.setattr(runtime_module.run_v2, "RevisionsClient", Mock())
    monkeypatch.setattr(runtime_module.httpx, "Client", Mock())
    runtime = runtime_module.GcpRuntime(sdk.settings)
    runtime.connect()
    runtime.connect()
    assert runtime.data is None and runtime.migrate is None
    assert runtime.redis.session is runtime.cache.session is session
    sessions.assert_called_once()
    runtime.close()
    session.close.assert_called_once()


def test_common_api_accepts_redis_and_plans_without_cloud_writes(redis_harness):
    h = redis_harness
    registry = ProviderRegistry()
    adapter = GcpAdapter(h.settings, store=h.store)
    registry.register(adapter)
    assert adapter.capabilities().features["redis"].stage == "sdk_only"
    with TestClient(create_app(registry=registry, store=h.store, operator_token=TOKEN)) as client:
        payload = h.request.model_dump(mode="json")
        plan = client.post("/api/v1/plans", json=payload, headers=HEADERS)
        assert plan.status_code == 200
        assert [s["id"] for s in plan.json()["steps"]] == [
            "validate",
            "cache",
            "deploy",
            "verify",
            "promote",
            "bake",
        ]
        release = client.post(
            "/api/v1/releases",
            headers=HEADERS,
            json={"request": payload, "idempotency_key": "gcp-redis-api-test-0001"},
        )
        assert release.status_code == 202
        assert release.json()["status"] == "queued"
    assert not h.api.writes and not h.redis_api.writes and not h.data_api.writes


def test_provision_reuse_and_restart_preserve_pinned_secrets_without_credentials(redis_harness):
    h = redis_harness
    binding = prepare_redis(h)
    assert binding["version"] == binding["ca_version"] == "1"
    url = h.redis_api.versions[binding["secret"]]["1"]
    parsed = urlsplit(url)
    assert parsed.scheme == "rediss" and parsed.hostname == "10.30.0.8" and parsed.port == 6378
    assert unquote(parsed.password) == AUTH
    assert parse_qs(parsed.query) == {
        "ssl_ca_certs": [REDIS_CA_FILE],
        "ssl_cert_reqs": ["required"],
        "ssl_check_hostname": ["false"],
    }
    assert h.redis_api.versions[binding["ca_secret"]]["1"] == CA
    checkpoint = jsonlib.dumps(redis_status(h.store, h.settings))
    assert AUTH not in checkpoint and CA not in checkpoint and "rediss://" not in checkpoint
    writes = deepcopy(h.redis_api.writes)
    h.runtime.redis = GcpRedisConnector(h.redis_api, h.settings)
    assert prepare_redis(h) == binding and h.redis_api.writes == writes
    assert not h.data_api.writes
    env = GcpAdapter(h.settings, store=h.store).public_environment()
    assert env["data_provisioning"]["configured"] is False
    assert env["cache_provisioning"]["configured"] is True
    assert env["request_template"]["resources"]["redis"] is True
    payload = h.request.model_dump(mode="json")
    payload["resources"]["redis"] = False
    with pytest.raises(ValueError, match="재사용"):
        GcpAdapter(h.settings, store=h.store).parse_request(payload)


def test_worker_waits_for_redis_before_candidate_then_injects_url_and_ca(redis_harness):
    h = redis_harness
    h.redis_api.pending = True
    row = enqueue(h)
    advance(h, row, {"data_preparing"})
    for _ in range(5):
        tick(h)
    assert not h.api.writes and not h.data_api.writes
    assert len(h.redis_api.executions) == 1
    h.redis_api.pending = False
    advance(h, row, {"redis_preparing"})
    done = advance(h, row, {"queued"})
    assert done["checkpoint"]["redis_binding"] and not h.api.writes
    advance(h, row, {"verifying"})
    template = h.api.service.template
    refs = {e.name: e for e in template.containers[0].env}
    assert refs["REDIS_URL"].value == ""
    assert refs["REDIS_URL"].value_source.secret_key_ref.version == "1"
    assert "DATABASE_URL" not in refs
    assert refs["DB_PASSWORD"].value_source.secret_key_ref.secret == "app-db"
    volume = next(v for v in template.volumes if v.name == REDIS_CA_VOLUME)
    assert volume.secret.secret.endswith("oneport_demo_redis_ca")
    assert volume.secret.items[0].version == "1" and volume.secret.items[0].path == "ca.pem"
    mount = next(m for m in template.containers[0].volume_mounts if m.name == REDIS_CA_VOLUME)
    assert mount.mount_path == REDIS_CA_DIR
    assert template.vpc_access.network_interfaces[0].network == h.settings.data_provisioning.network


@pytest.mark.parametrize("failure", ["workflow", "api"])
def test_redis_failure_leaves_stable_service_untouched(redis_harness, failure):
    h = redis_harness
    before = deepcopy(h.api.service)
    if failure == "workflow":
        h.redis_api.failed = True
    else:
        h.redis_api.fail_status = 403
    row = enqueue(h)
    result = advance(h, row, {"failed"})
    assert result["error_code"] == "gcp_cache_preparation_failed"
    assert not h.api.writes and h.api.service == before
    assert "SENSITIVE" not in str(result)


@pytest.mark.parametrize("response", ["ca", "url"])
def test_lost_write_response_recovers_without_resubmission(redis_harness, response):
    h = redis_harness
    h.redis_api.lose_secret_response = response
    with pytest.raises(ProviderUnavailable):
        prepare_redis(h)
    h.runtime.redis = GcpRedisConnector(h.redis_api, h.settings)
    assert prepare_redis(h)["version"] == "1"
    assert len(h.redis_api.executions) == 1
    assert all(len(versions) == 1 for versions in h.redis_api.versions.values())


@pytest.mark.parametrize("change", ["owner", "network", "auth", "tls", "identity"])
def test_redis_drift_is_never_adopted_or_replaced(redis_harness, change):
    h = redis_harness
    prepare_redis(h)
    writes = deepcopy(h.redis_api.writes)
    if change == "owner":
        h.redis_api.instance["labels"]["app_id"] = "other"
    elif change == "network":
        h.redis_api.instance["authorizedNetwork"] += "-external"
    elif change == "auth":
        h.redis_api.instance["authEnabled"] = False
    elif change == "tls":
        h.redis_api.instance["transitEncryptionMode"] = "DISABLED"
    else:
        h.redis_api.instance["createTime"] = "2026-10-04T01:00:00Z"
    with pytest.raises(DataProvisionError):
        prepare_redis(h)
    assert h.redis_api.writes == writes


def test_external_secret_cannot_be_overwritten(redis_harness):
    h = redis_harness
    h.redis_api.secrets[h.runtime.redis.secret_names["ca"]] = {"labels": {"managed_by": "other"}}
    with pytest.raises(DataProvisionError, match="외부 Redis Secret"):
        prepare_redis(h)
    assert h.redis_api.secrets[h.runtime.redis.secret_names["ca"]] == {
        "labels": {"managed_by": "other"}
    }
    assert not any(path.endswith("redis_ca:addVersion") for path, _ in h.redis_api.writes)


def test_external_cloud_run_url_and_volume_are_preserved(redis_harness):
    h = redis_harness
    h.provider.redis_binding = prepare_redis(h)
    before = deepcopy(h.api.service)
    h.api.service.template.containers[0].env.append(
        run_v2.EnvVar(name="REDIS_URL", value="external")
    )
    with pytest.raises(ProviderConflict, match="external REDIS_URL"):
        h.provider.snapshot(h.request, "a" * 24)
    h.api.service = before
    h.api.service.template.volumes.append(
        run_v2.Volume(name=REDIS_CA_VOLUME, secret=run_v2.SecretVolumeSource(secret="external"))
    )
    with pytest.raises(ProviderConflict, match="external Redis CA"):
        h.provider.snapshot(h.request, "a" * 24)
    assert not h.api.writes


def test_external_redis_url_stops_provisioning_before_any_cloud_write(redis_harness):
    h = redis_harness
    h.api.service.template.containers[0].env.append(
        run_v2.EnvVar(name="REDIS_URL", value="external")
    )
    row = enqueue(h)
    result = advance(h, row, {"failed"})
    assert result["error_code"] == "gcp_cache_preparation_failed"
    assert not h.api.writes and not h.redis_api.writes


def test_connection_failure_preserves_stable_before_migration_or_candidate(redis_harness):
    h = redis_harness
    before = deepcopy(h.api.service)
    row = enqueue(h)
    advance(h, row, {"redis_preparing"})
    h.redis_api.fail_status = 403
    result = advance(h, row, {"failed"})
    assert result["error_code"] == "gcp_redis_preparation_failed"
    assert h.api.service == before and not h.api.writes


def test_combined_db_redis_migration_blocks_candidate_until_all_ready(migration_harness):  # noqa: F811
    h = migration_harness
    h.request.resources.redis = True
    enable_connection(h)
    adapter = GcpAdapter(h.settings, store=h.store)
    assert [s.id for s in adapter.plan_steps(h.request)] == [
        "validate",
        "data",
        "cache",
        "migrate",
        "deploy",
        "verify",
        "promote",
        "bake",
    ]
    row = enqueue(h)
    advance(h, row, {"redis_preparing"})
    assert not h.jobs_api.writes and not h.api.writes
    advance(h, row, {"migrating"})
    assert not h.api.writes
    advance(h, row, {"verifying"})
    refs = {e.name: e for e in h.api.service.template.containers[0].env}
    assert refs["DATABASE_URL"].value_source.secret_key_ref.version == "1"
    assert refs["REDIS_URL"].value_source.secret_key_ref.version == "1"
    assert len(h.jobs_api.executions) == 1


def test_health_rollback_and_second_release_keep_cache_and_ca(redis_harness):
    h = redis_harness
    row = enqueue(h)
    advance(h, row, {"baking"})
    h.runtime.probe.side_effect = lambda *args, release, **kwargs: release != row["id"]
    result = advance(h, row, {"failed"})
    assert result["error_code"] == "health_check_failed_restored"
    assert weights(h.api.service) == {"demo-v1": 100}
    assert redis_status(h.store, h.settings)["phase"] == "ready"
    writes = deepcopy(h.redis_api.writes)
    h.runtime.probe.side_effect = None
    second = h.store.enqueue(
        h.request,
        "gcp-redis-second-release-0001",
        h.settings.fingerprint(),
        runtime=h.worker.queue_runtime,
        timeout_seconds=7200,
    )
    advance(h, second, {"verifying"})
    assert h.redis_api.writes == writes
    assert (
        h.store.get(second["id"])["checkpoint"]["redis_binding"]
        == result["checkpoint"]["redis_binding"]
    )


def test_redis_deadline_never_deploys_candidate(redis_harness):
    h = redis_harness
    row = enqueue(h)
    advance(h, row, {"redis_preparing"})
    tick(h)
    with h.store.connect() as conn:
        conn.execute("UPDATE releases SET deadline=now()-interval '1 second'")
    result = advance(h, row, {"failed"})
    assert result["error_code"] == "deadline_exceeded"
    assert not h.api.writes


@pytest.mark.parametrize("delete_data", [False, True])
def test_app_deletion_preserves_redis_and_reports_retained_resources(redis_harness, delete_data):
    from test_app_deletion import request_deletion, run_deletion

    h = redis_harness
    prepare_redis(h)
    checkpoint = redis_status(h.store, h.settings)
    writes = deepcopy(h.redis_api.writes)
    info = GcpAdapter(h.settings, store=h.store).deletion_info()
    assert any("Redis" in item for item in info["keeps"])
    request_deletion(
        h.store,
        "gcp-test",
        Runtime.GCP_CLOUD_RUN,
        "demo",
        "gcp-redis-delete-test-0001",
        delete_data=delete_data,
    )
    result = run_deletion(h.store, "gcp-test", h.worker.tick)
    assert result["state"] == "deleted", result["events"]
    assert redis_status(h.store, h.settings) == checkpoint
    assert h.redis_api.writes == writes
    assert h.api.writes[-1] == (["delete"], None)


@pytest.mark.parametrize("state", ["redis_preparing", "migrating"])
def test_deletion_cannot_interrupt_redis_or_migration(redis_harness, state):
    h = redis_harness
    enqueue(h)
    with h.store.connect() as conn:
        conn.execute("UPDATE releases SET state=%s", (state,))
    with pytest.raises(RequestConflict, match="진행 중"):
        DeletionStore(h.store.dsn).request(
            application="demo",
            environment_id="gcp-test",
            runtime=Runtime.GCP_CLOUD_RUN,
            delete_data=True,
            key="gcp-redis-busy-delete-0001",
        )
    assert not h.api.writes and not h.redis_api.writes
