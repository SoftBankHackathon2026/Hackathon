import base64
import json
from copy import deepcopy
from json import dumps
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from google.cloud import run_v2
from requests import ConnectionError, ConnectTimeout
from test_gcp import advance, enqueue, tick
from test_gcp import harness as harness
from test_gcp import sdk as sdk
from test_gcp_data import data_harness as data_harness
from urllib3.exceptions import MaxRetryError, NewConnectionError

from controlplane.execution.gcp.cache import (
    CacheSubmissionUnknown,
    GcpCacheProvisioner,
    cache_status,
)
from controlplane.execution.gcp.data import DataProvisionError
from controlplane.execution.gcp.settings import GcpCacheSettings, GcpSettings
from controlplane.providers.contracts import ProviderUnavailable
from controlplane.providers.gcp.adapter import GcpAdapter
from controlplane.providers.gcp.models import GcpDeploymentRequest
from controlplane.providers.interface import DeploymentSpec


def test_gcp_accepts_redis_request_without_requiring_postgres(sdk):
    payload = sdk.request.model_dump(mode="json")
    payload["resources"]["redis"] = True
    request = GcpDeploymentRequest.model_validate(payload)
    assert request.resources.redis and request.resources.database == "none"
    with pytest.raises(ValueError, match="캐시 생성 설정"):
        sdk.settings.validate_request(request)


class CacheApi:
    def __init__(self, provisioner):
        self.provisioner = provisioner
        self.execution = None
        self.secret = None
        self.versions = {}
        self.writes = []
        self.lose_submission = False
        self.lose_version = False
        self.fail_status = None
        self.version_state = "ENABLED"
        self.instance = {
            "name": provisioner.instance,
            "state": "READY",
            "labels": {"managed_by": "oneport-cache-workflow", "app_id": "demo"},
            "authorizedNetwork": provisioner.policy.network,
            "connectMode": "PRIVATE_SERVICE_ACCESS",
            "authEnabled": True,
            "transitEncryptionMode": "SERVER_AUTHENTICATION",
            "tier": "BASIC",
            "memorySizeGb": 1,
            "host": "10.30.0.4",
            "port": 6378,
            "serverCaCerts": [{"cert": "TEST_CA"}],
        }

    def request(self, method, url, *, json=None, timeout=None):
        def response(body, status=200):
            return SimpleNamespace(status_code=status, json=lambda: deepcopy(body))

        p = self.provisioner
        path = urlsplit(url).path
        if self.fail_status:
            return response({"error": "SENSITIVE_CLOUD_BODY"}, self.fail_status)
        if method == "GET":
            if "/executions/" in path:
                return response(self.execution)
            if path.endswith("/authString"):
                return response({"authString": "DO_NOT_PERSIST_PASSWORD"})
            if "/instances/" in path:
                return response(self.instance)
            if "/versions/" in path:
                return response({"state": self.version_state})
            if "/secrets/" in path:
                return response(self.secret, 200 if self.secret else 404)
        assert method == "POST", (method, path)
        self.writes.append((path, deepcopy(json)))
        if path.endswith("/executions"):
            self.execution = {
                "name": p.executions + "/execution-1",
                "argument": json["argument"],
                "state": "SUCCEEDED",
                "result": dumps(
                    {
                        "schema_version": 1,
                        "action": "create",
                        "app_id": "demo",
                        "instance": p.instance,
                        "state": "READY",
                    }
                ),
            }
            if self.lose_submission:
                raise ConnectionError("SENSITIVE_CLOUD_BODY")
            return response(self.execution)
        if path.endswith("/secrets"):
            self.secret = json
            return response({})
        if path.endswith(":addVersion"):
            version = str(len(self.versions) + 1)
            self.versions[version] = base64.b64decode(json["payload"]["data"]).decode()
            if self.lose_version:
                self.lose_version = False
                raise ConnectionError("SENSITIVE_CLOUD_BODY")
            return response({"name": p.secret + "/versions/" + version})
        raise AssertionError(path)


def enable_cache(h):
    h.settings.cache_provisioning = GcpCacheSettings(
        app_id="demo",
        network="projects/test-project/global/networks/oneport",
        subnetwork="projects/test-project/regions/asia-northeast3/subnetworks/oneport-cloud-run",
    )
    payload = h.request.model_dump(mode="json")
    payload["resources"]["redis"] = True
    payload["strategy"] = "blue_green"
    h.request = GcpDeploymentRequest.model_validate(payload)
    h.api.service.template.service_account = "oneport-rt-demo@test-project.iam.gserviceaccount.com"
    h.api.service.template.vpc_access = run_v2.VpcAccess()
    h.runtime.cache = GcpCacheProvisioner(None, h.settings)
    h.cache_api = CacheApi(h.runtime.cache)
    h.runtime.cache.session = h.cache_api
    # Exercise the production worker's URL/CA stage while retaining this file's
    # CACHE_CONFIG mock and regression assertions.
    from test_gcp_redis import RedisApi

    from controlplane.execution.gcp.redis import GcpRedisConnector

    h.redis_api = RedisApi(h.settings)
    h.redis_api.instance = h.redis_api.ready_instance()
    h.runtime.redis = GcpRedisConnector(h.redis_api, h.settings)
    return h


@pytest.fixture
def cache_harness(harness):
    return enable_cache(harness)


def prepare(h):
    with h.store.worker_lock() as conn:
        for _ in range(10):
            _, binding = h.runtime.cache.step(conn)
            if binding:
                return binding
    pytest.fail("Cache preparation did not complete")


def test_menu_capability_request_and_plan_enable_cache_without_cloud_writes(cache_harness):
    h = cache_harness
    adapter = GcpAdapter(h.settings, store=h.store)
    assert adapter.capabilities().features["redis"].stage == "sdk_only"
    assert adapter.public_environment()["cache_provisioning"] == {
        "enabled": True,
        "configured": False,
        "state": "none",
    }
    request = adapter.parse_request(h.request.model_dump(mode="json"))
    assert "cache" in [s.id for s in adapter.plan_steps(request)]
    spec = DeploymentSpec.model_validate(h.request.model_dump(mode="json"))
    assert "cache" in [s.id for s in adapter.plan(spec).steps]
    operation = adapter.submit(spec, "gcp-cache-common-sdk-0001")
    assert adapter.observe(operation).status == "queued"
    assert not h.cache_api.writes and not h.api.writes


def test_cache_reuses_pinned_secret_across_restart_and_keeps_credentials_out_of_journal(
    cache_harness,
):
    h = cache_harness
    binding = prepare(h)
    assert binding["version"] == "1"
    assert json.loads(h.cache_api.versions["1"]) == {
        "schema_version": 1,
        "host": "10.30.0.4",
        "port": 6378,
        "password": "DO_NOT_PERSIST_PASSWORD",
        "ca_pem": "TEST_CA",
    }
    saved = json.dumps(cache_status(h.store, h.settings))
    assert "DO_NOT_PERSIST_PASSWORD" not in saved and "TEST_CA" not in saved
    writes = deepcopy(h.cache_api.writes)
    h.runtime.cache = GcpCacheProvisioner(h.cache_api, h.settings)
    assert prepare(h) == binding and h.cache_api.writes == writes
    adapter = GcpAdapter(h.settings, store=h.store)
    assert adapter.public_environment()["request_template"]["resources"]["redis"] is True
    payload = h.request.model_dump(mode="json")
    payload["resources"]["redis"] = False
    with pytest.raises(ValueError, match="재사용"):
        adapter.parse_request(payload)


def test_pending_workflow_is_polled_after_restart_without_resubmission(cache_harness):
    h = cache_harness
    with h.store.worker_lock() as conn:
        h.runtime.cache.step(conn)
        h.cache_api.execution["state"] = "ACTIVE"
        h.runtime.cache = GcpCacheProvisioner(h.cache_api, h.settings)
        for _ in range(3):
            assert h.runtime.cache.step(conn) == ("creating", None)
    assert len(h.cache_api.writes) == 1
    h.cache_api.execution["state"] = "SUCCEEDED"
    assert prepare(h)["version"] == "1"


def test_lost_workflow_response_never_repeats_post(cache_harness):
    h = cache_harness
    h.cache_api.lose_submission = True
    with pytest.raises(ProviderUnavailable):
        prepare(h)
    h.runtime.cache = GcpCacheProvisioner(h.cache_api, h.settings)
    with pytest.raises(CacheSubmissionUnknown):
        prepare(h)
    assert len(h.cache_api.writes) == 1


def throttled():
    return SimpleNamespace(status_code=429, json=dict)


def refused():
    raise ConnectionError(MaxRetryError(None, "/executions", NewConnectionError(None, "refused")))


def connect_timeout():
    raise ConnectTimeout("connect timed out")


@pytest.mark.parametrize("undelivered", [throttled, refused, connect_timeout])
def test_undelivered_workflow_submission_is_submitted_again(cache_harness, undelivered):
    h = cache_harness
    api, failed = h.cache_api, []

    def request(method, url, **kwargs):
        if method == "POST" and url.endswith("/executions") and not failed:
            failed.append(url)
            return undelivered()
        return api.request(method, url, **kwargs)

    h.runtime.cache.session = SimpleNamespace(request=request)
    with pytest.raises(ProviderUnavailable):
        prepare(h)
    assert cache_status(h.store, h.settings)["phase"] == "new"
    assert prepare(h)["version"] == "1"
    assert [path for path, _ in api.writes if path.endswith("/executions")] == [
        urlsplit(failed[0]).path
    ]


def test_lost_secret_response_only_retries_unpublished_version(cache_harness):
    h = cache_harness
    h.cache_api.lose_version = True
    with pytest.raises(ProviderUnavailable):
        prepare(h)
    assert prepare(h)["version"] == "2"
    assert h.cache_api.versions["1"] == h.cache_api.versions["2"]
    assert sum(path.endswith("/executions") for path, _ in h.cache_api.writes) == 1


@pytest.mark.parametrize("database", [False, True])
def test_worker_connects_cache_and_database_before_candidate(data_harness, database):
    h = enable_cache(data_harness)
    if not database:
        h.request.resources.database = "none"
    row = enqueue(h)
    advance(h, row, {"baking"})
    values = {e.name: e for e in h.api.service.template.containers[0].env}
    value = values["CACHE_CONFIG"]
    assert not value.value and value.value_source.secret_key_ref.version == "1"
    assert value.value_source.secret_key_ref.secret.endswith("/oneport_demo_cache")
    if database:
        assert values["DATABASE_URL"].value_source.secret_key_ref.version == "1"
    assert h.api.service.template.vpc_access.network_interfaces[0].network.endswith("/oneport")
    h.clock += 15
    result = advance(h, row, {"succeeded"})
    assert "DO_NOT_PERSIST_PASSWORD" not in str(result)
    writes = deepcopy(h.cache_api.writes)
    second = h.store.enqueue(
        h.request,
        "gcp-cache-redeploy-0002",
        h.settings.fingerprint(),
        runtime="gcp_cloud_run",
        timeout_seconds=7200,
    )
    advance(h, second, {"baking"})
    assert h.cache_api.writes == writes


@pytest.mark.parametrize("failure", ["permission", "lost_response", "external_secret", "workflow"])
def test_cache_failure_preserves_production_and_does_not_leak_secrets(cache_harness, failure):
    h = cache_harness
    if failure == "permission":
        h.cache_api.fail_status = 403
    elif failure == "lost_response":
        h.cache_api.lose_submission = True
    elif failure == "external_secret":
        h.api.service.template.containers[0].env.append(
            run_v2.EnvVar(name="CACHE_CONFIG", value="external")
        )
    row = enqueue(h)
    if failure == "workflow":
        tick(h)
        tick(h)
        h.cache_api.execution["state"] = "FAILED"
    result = advance(h, row, {"failed", "uncertain"})
    assert result["state"] == ("uncertain" if failure == "lost_response" else "failed")
    assert not h.api.writes
    assert "SENSITIVE_CLOUD_BODY" not in str(result)
    if failure == "external_secret":
        assert not h.cache_api.writes


@pytest.mark.parametrize("mutation", ["owner", "network", "auth", "version", "secret_owner"])
def test_reuse_rejects_changed_cache_or_secret(cache_harness, mutation):
    h = cache_harness
    prepare(h)
    writes = deepcopy(h.cache_api.writes)
    if mutation == "owner":
        h.cache_api.instance["labels"]["app_id"] = "other"
    elif mutation == "network":
        h.cache_api.instance["authorizedNetwork"] = "other"
    elif mutation == "auth":
        h.cache_api.instance["authEnabled"] = False
    elif mutation == "version":
        h.cache_api.version_state = "DISABLED"
    else:
        h.cache_api.secret["labels"]["app_id"] = "other"
    with pytest.raises(DataProvisionError):
        prepare(h)
    assert h.cache_api.writes == writes


def test_cache_opt_in_preserves_release_fingerprint_and_requires_matching_network(sdk):
    previous = sdk.settings.fingerprint()
    settings = sdk.settings.model_dump(mode="json")
    settings["cache_provisioning"] = {
        "app_id": "demo",
        "network": "projects/test-project/global/networks/oneport",
        "subnetwork": "projects/test-project/regions/asia-northeast3/subnetworks/oneport-cloud-run",
    }
    assert GcpSettings.model_validate(settings).fingerprint() == previous
    settings["cache_provisioning"]["network"] = "projects/other-project/global/networks/oneport"
    with pytest.raises(ValueError, match="registered project"):
        GcpSettings.model_validate(settings)


@pytest.mark.parametrize("change", ["argument", "result"])
def test_workflow_receipt_must_match_requested_app(cache_harness, change):
    h = cache_harness
    with h.store.worker_lock() as conn:
        h.runtime.cache.step(conn)
        value = json.loads(h.cache_api.execution[change])
        value["app_id"] = "other"
        h.cache_api.execution[change] = json.dumps(value)
        with pytest.raises(DataProvisionError, match="요청"):
            h.runtime.cache.step(conn)
    assert h.cache_api.secret is None and not h.cache_api.versions


def test_candidate_failure_keeps_cache_and_original_traffic(cache_harness):
    h = cache_harness
    row = enqueue(h)
    h.runtime.probe.side_effect = lambda *a, release, **k: release != row["id"]
    result = advance(h, row, {"failed"})
    assert result["error_code"] == "candidate_unhealthy_restored"
    assert cache_status(h.store, h.settings)["phase"] == "ready"
    assert h.cache_api.versions and h.api.service.traffic_statuses[0].revision == "demo-v1"


def test_changing_cache_policy_during_promotion_stops_cloud_writes(cache_harness):
    h = cache_harness
    row = enqueue(h)
    advance(h, row, {"baking"})
    writes = deepcopy(h.api.writes)
    h.settings.cache_provisioning.network = "projects/test-project/global/networks/other"
    h.runtime.cache = GcpCacheProvisioner(h.cache_api, h.settings)
    result = advance(h, row, {"uncertain"})
    assert result["error_code"] == "ProviderConflict"
    assert h.api.writes == writes


def test_app_deletion_explicitly_retains_cache_even_when_deleting_database(cache_harness):
    from test_app_deletion import request_deletion, run_deletion

    h = cache_harness
    prepare(h)
    info = GcpAdapter(h.settings, store=h.store).deletion_info()
    assert any("Redis" in item for item in info["keeps"])
    assert not any("Redis" in item for item in info["data"])
    writes = deepcopy(h.cache_api.writes)
    request_deletion(
        h.store, "gcp-test", "gcp_cloud_run", "demo", "cache-app-delete-0001", delete_data=True
    )
    result = run_deletion(h.store, "gcp-test", h.worker.tick)
    assert result["state"] == "deleted"
    assert cache_status(h.store, h.settings)["phase"] == "ready"
    assert h.cache_api.writes == writes
