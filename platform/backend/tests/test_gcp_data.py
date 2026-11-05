import base64
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from google.cloud import run_v2
from requests import ConnectionError
from test_gcp import advance, enqueue, tick
from test_gcp import harness as harness
from test_gcp import sdk as sdk

from controlplane.domain.models import ResourceRequirements, Strategy
from controlplane.execution.gcp.data import DataProvisionError, GcpDataProvisioner, data_status
from controlplane.execution.gcp.settings import GcpDataSettings
from controlplane.providers.contracts import ProviderConflict, ProviderUnavailable
from controlplane.providers.gcp.adapter import GcpAdapter
from controlplane.providers.gcp.models import GcpDeploymentRequest


class DataApi:
    """REST-shaped fake: captures mutations and can lose a committed response."""

    def __init__(self):
        self.instance = None
        self.database = None
        self.secret = None
        self.versions = {}
        self.users = {}
        self.writes = []
        self.lose_user_response = False
        self.pending_operation = False
        self.failed_operation = False
        self.fail_status = None

    def request(self, method, url, *, json=None, timeout=None):
        def response(body, status=200):
            return SimpleNamespace(status_code=status, json=lambda: deepcopy(body))

        path = urlsplit(url).path
        if self.fail_status:
            return response({"error": "DO_NOT_EXPOSE_CLOUD_PAYLOAD"}, self.fail_status)
        if method == "GET":
            if "/operations/" in path:
                return response(
                    {
                        "status": "RUNNING" if self.pending_operation else "DONE",
                        **({"error": {"message": "SENSITIVE"}} if self.failed_operation else {}),
                    }
                )
            if "/versions/" in path:
                return response({"state": "ENABLED"})
            if path.endswith("/databases/app"):
                return response(self.database, 200 if self.database else 404)
            if path.endswith("/users"):
                return response({"items": [{"name": name} for name in self.users]})
            if "/secrets/" in path:
                return response(self.secret, 200 if self.secret else 404)
            return response(self.instance, 200 if self.instance else 404)
        assert method == "POST"
        self.writes.append((path, deepcopy(json)))
        if path.endswith("/instances"):
            self.instance = {
                **json,
                "state": "RUNNABLE",
                "ipAddresses": [{"type": "PRIVATE", "ipAddress": "10.30.0.3"}],
            }
        elif path.endswith("/databases"):
            self.database = json
        elif path.endswith("/secrets"):
            self.secret = json
            return response(json)
        elif path.endswith(":addVersion"):
            version = str(len(self.versions) + 1)
            self.versions[version] = base64.b64decode(json["payload"]["data"]).decode()
            return response({"name": path + "/versions/" + version})
        elif path.endswith("/users"):
            self.users[json["name"]] = json["password"]
            if self.lose_user_response:
                self.lose_user_response = False
                raise ConnectionError("SENSITIVE transport payload")
        else:
            raise AssertionError(path)
        return response({"name": "op-" + str(len(self.writes))})


@pytest.fixture
def data_harness(harness):
    h = harness
    h.settings.data_provisioning = GcpDataSettings(
        app_id="demo",
        network="projects/test-project/global/networks/oneport",
        subnetwork="projects/test-project/regions/asia-northeast3/subnetworks/oneport-cloud-run",
        allocated_ip_range="oneport-private-services",
    )
    h.request = h.request.model_copy(
        update={
            "resources": ResourceRequirements(database="postgresql", database_mode="dedicated"),
            "strategy": Strategy.BLUE_GREEN,
        }
    )
    h.api.service.template.service_account = "oneport-rt-demo@test-project.iam.gserviceaccount.com"
    h.api.service.template.vpc_access = run_v2.VpcAccess()
    h.data_api = DataApi()
    h.runtime.data = GcpDataProvisioner(h.data_api, h.settings)
    return h


def prepare(h, limit=25):
    with h.store.worker_lock() as conn:
        for _ in range(limit):
            phase, binding = h.runtime.data.step(conn)
            if binding:
                return binding
    pytest.fail("DB preparation did not complete")


def test_plan_and_environment_enable_dedicated_database_without_cloud_writes(data_harness):
    h = data_harness
    adapter = GcpAdapter(h.settings, store=h.store)
    assert adapter.capabilities().features["database"].stage == "sdk_only"
    assert adapter.capabilities().database_modes == ["dedicated"]
    env = adapter.public_environment()
    assert env["data_provisioning"] == {"enabled": True, "configured": False, "state": "none"}
    assert env["request_template"]["resources"]["database_mode"] == "dedicated"
    adapter.parse_request(h.request.model_dump(mode="json"))
    assert "data" in [s.id for s in adapter.plan_steps(h.request)]
    assert not h.data_api.writes


def test_postgres_requires_operator_policy_and_dedicated_mode(sdk):
    request = sdk.request.model_dump(mode="json")
    request["resources"].update(database="postgresql", database_mode="dedicated")
    parsed = GcpDeploymentRequest.model_validate(request)
    with pytest.raises(ValueError, match="DB 생성 설정"):
        sdk.settings.validate_request(parsed)
    request["resources"]["database_mode"] = "shared"
    with pytest.raises(ValueError, match="dedicated"):
        GcpDeploymentRequest.model_validate(request)


def test_enabling_database_does_not_invalidate_existing_release_fingerprint(data_harness):
    h = data_harness
    previous = h.settings.model_dump(
        mode="json",
        exclude={
            "database_url",
            "allowed_images",
            "data_provisioning",
            "cache_provisioning",
            "tracing",
            "credentials_file",
            "catalog_target_id",
            "catalog_dynamic",
            "catalog_owner",
        },
    )
    previous.pop("service_access")
    assert (
        h.settings.fingerprint()
        == hashlib.sha256(json.dumps(previous, sort_keys=True).encode()).hexdigest()
    )


def test_provision_once_reuse_pinned_secret_and_never_persist_password(data_harness):
    h = data_harness
    binding = prepare(h)
    assert h.data_api.instance["settings"]["ipConfiguration"]["ipv4Enabled"] is False
    assert h.data_api.instance["settings"]["deletionProtectionEnabled"] is True
    user, password = next(iter(h.data_api.users.items()))
    assert (
        h.data_api.versions[binding["version"]]
        == f"postgresql://{user}:{password}@10.30.0.3:5432/app?sslmode=require"
    )
    saved = json.dumps(data_status(h.store, h.settings))
    assert password not in saved and "postgresql://" not in saved
    writes = deepcopy(h.data_api.writes)
    h.runtime.data = GcpDataProvisioner(h.data_api, h.settings)  # worker restart
    assert prepare(h) == binding
    assert h.data_api.writes == writes
    adapter = GcpAdapter(h.settings, store=h.store)
    assert adapter.public_environment()["request_template"]["resources"]["database"] == "postgresql"
    disabled = h.request.model_dump(mode="json")
    disabled["resources"]["database"] = "none"
    with pytest.raises(ValueError, match="재사용"):
        adapter.parse_request(disabled)


def test_lost_user_create_response_recovers_without_rotating_password(data_harness):
    h = data_harness
    h.data_api.lose_user_response = True
    with pytest.raises(ProviderUnavailable, match="transport"):
        prepare(h)
    users = dict(h.data_api.users)
    h.runtime.data = GcpDataProvisioner(h.data_api, h.settings)
    assert prepare(h)["version"] == "1"
    assert h.data_api.users == users and len(h.data_api.versions) == 1


def test_restart_before_user_create_abandons_only_unpublished_credentials(data_harness):
    h = data_harness
    original = h.data_api.request

    def crash(method, url, **kwargs):
        if method == "POST" and url.endswith("/users"):
            raise ConnectionError("crash before write")
        return original(method, url, **kwargs)

    h.data_api.request = crash
    with pytest.raises(ProviderUnavailable):
        prepare(h)
    assert not h.data_api.users
    h.data_api.request = original
    h.runtime.data = GcpDataProvisioner(h.data_api, h.settings)
    assert prepare(h)["version"] == "2"
    assert len(h.data_api.users) == 1


def test_pending_cloud_sql_operation_waits_and_does_not_create_duplicate(data_harness):
    h = data_harness
    h.data_api.pending_operation = True
    with h.store.worker_lock() as conn:
        for _ in range(5):
            assert h.runtime.data.step(conn) == ("instance", None)
    assert len(h.data_api.writes) == 1
    h.data_api.pending_operation = False
    assert prepare(h)["version"] == "1"


def test_foreign_database_and_changed_policy_are_never_adopted(data_harness):
    h = data_harness
    prepare(h)
    writes = len(h.data_api.writes)
    h.data_api.instance["settings"]["userLabels"] = {"managed-by": "someone-else"}
    with pytest.raises(DataProvisionError, match="소유권"):
        prepare(h)
    assert len(h.data_api.writes) == writes
    h.settings.data_provisioning.app_id = "other"
    h.runtime.data = GcpDataProvisioner(h.data_api, h.settings)
    with pytest.raises(DataProvisionError, match="변경"):
        prepare(h)


def test_worker_creates_data_then_candidate_with_secret_and_vpc(data_harness):
    h = data_harness
    row = enqueue(h)
    tick(h)
    assert h.store.get(row["id"])["state"] == "data_preparing"
    assert not h.api.writes and not h.data_api.writes
    advance(h, row, {"baking"})
    template = h.api.service.template
    value = next(e for e in template.containers[0].env if e.name == "DATABASE_URL")
    assert value.value == "" and value.value_source.secret_key_ref.version == "1"
    assert template.vpc_access.network_interfaces[0].subnetwork.endswith("/oneport-cloud-run")
    assert template.vpc_access.egress == run_v2.VpcAccess.VpcEgress.PRIVATE_RANGES_ONLY
    h.clock += 15
    assert advance(h, row, {"succeeded"})["service_url"] == h.settings.environment.service_url
    events = str(h.store.get(row["id"])["events"])
    assert next(iter(h.data_api.users.values())) not in events


def test_data_failure_leaves_live_app_untouched_and_error_is_safe(data_harness):
    h = data_harness
    h.data_api.fail_status = 403
    row = enqueue(h)
    result = advance(h, row, {"failed"})
    assert result["error_code"] == "gcp_data_preparation_failed"
    assert not h.api.writes
    assert "403" in str(result["events"])
    assert "DO_NOT_EXPOSE" not in str(result)


def test_candidate_refuses_to_overwrite_external_database(data_harness):
    h = data_harness
    h.provider.data_binding = prepare(h)
    h.api.service.template.containers[0].env.append(
        run_v2.EnvVar(name="DATABASE_URL", value="external")
    )
    with pytest.raises(ProviderConflict, match="external DATABASE_URL"):
        h.provider.snapshot(h.request, "a" * 24)


@pytest.mark.parametrize("candidate_healthy", [True, False])
def test_new_ready_path_does_not_break_baseline_or_rollback(data_harness, candidate_healthy):
    h = data_harness
    old = h.store.enqueue(
        h.request.model_copy(
            update={"resources": ResourceRequirements(), "health_path": "/health"}
        ),
        "previous-gcp-release-0001",
        h.settings.fingerprint(),
        runtime="gcp_cloud_run",
        timeout_seconds=7200,
    )
    with h.store.connect() as conn:
        conn.execute("UPDATE releases SET state='succeeded' WHERE id=%s", (old["id"],))
    stable = next(iter(h.api.revisions.values()))
    stable.containers[0].env.append(run_v2.EnvVar(name="LAUNCHPAD_RELEASE_ID", value=old["id"]))
    h.request = h.request.model_copy(update={"health_path": "/ready"})
    h.runtime.probe.side_effect = lambda request, service, *, url, release: (
        request.health_path == "/health"
        if release == old["id"]
        else candidate_healthy and request.health_path == "/ready"
    )
    row = enqueue(h)
    if candidate_healthy:
        advance(h, row, {"baking"})
        h.clock += 15
        assert advance(h, row, {"succeeded"})["state"] == "succeeded"
    else:
        result = advance(h, row, {"failed"})
        assert result["error_code"] == "candidate_unhealthy_restored"
        assert data_status(h.store, h.settings)["phase"] == "ready"
