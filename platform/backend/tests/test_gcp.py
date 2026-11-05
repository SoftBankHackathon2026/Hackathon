from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from cloud_run_fakes import LATEST, REVISION, CloudRunApi
from fastapi.testclient import TestClient
from google.cloud import run_v2
from pydantic import SecretStr, ValidationError

from controlplane.core.config import operator_token_from_environment
from controlplane.domain.models import RolloutPolicy, Runtime, Strategy
from controlplane.execution.gcp.runtime import GcpRuntime
from controlplane.execution.gcp.settings import GcpSettings
from controlplane.execution.gcp.worker import GcpWorker
from controlplane.main import create_app
from controlplane.providers.contracts import ProviderConflict, SubmissionUncertain
from controlplane.providers.gcp.adapter import GcpAdapter
from controlplane.providers.gcp.models import GcpDeploymentRequest
from controlplane.providers.gcp.provider import CloudRunProvider, weights
from controlplane.providers.interface import DeploymentProvider, DeploymentSpec
from controlplane.providers.loader import load_platform
from controlplane.providers.onprem.models import ArgoDeploymentRequest
from controlplane.providers.registry import ProviderRegistry
from controlplane.services.deployments import DeploymentService

IMAGE = "asia-northeast3-docker.pkg.dev/test-project/apps/demo@sha256:" + "a" * 64
OLD_IMAGE = "asia-northeast3-docker.pkg.dev/test-project/apps/demo@sha256:" + "b" * 64
TOKEN = "oneport-gcp-test-operator-token-0123456789"
HEADERS = {"Authorization": "Bearer " + TOKEN}
TERMINAL = {"succeeded", "failed", "uncertain"}


@pytest.mark.parametrize("cpu,memory", [("1", 768), ("1", 1536), ("2", 1280)])
def test_intermediate_sizes_reach_cloud_run_revision(sdk, cpu, memory):
    request = GcpDeploymentRequest.model_validate(
        sdk.request.model_dump() | {"cpu": cpu, "memory_mib": memory}
    )
    template = sdk.provider.candidate_template(
        sdk.provider.read(), request, "demo-resource-test", "a" * 24
    )
    assert template.containers[0].resources.limits == {"cpu": cpu, "memory": f"{memory}Mi"}


@pytest.mark.parametrize("bundle", [True, False])
def test_native_gcp_configuration_uses_common_sdk(harness, tmp_path, monkeypatch, bundle):
    import json

    h = harness
    runtime = tmp_path / "runtime.json"
    value = h.settings.model_dump(mode="json")
    value["database_url"] = h.store.dsn
    runtime.write_text(json.dumps(value))
    monkeypatch.setenv("ONEPORT_OPERATOR_TOKEN", TOKEN)
    monkeypatch.delenv("ONEPORT_CONFIG", raising=False)
    monkeypatch.delenv("ONEPORT_RUNTIME_CONFIG", raising=False)
    if bundle:
        config = tmp_path / "platform.json"
        config.write_text(
            json.dumps(
                {
                    "database_url": h.store.dsn,
                    "providers": [{"runtime": "gcp_cloud_run", "config_file": "runtime.json"}],
                }
            )
        )
        monkeypatch.setenv("ONEPORT_CONFIG", str(config))
    else:
        monkeypatch.setenv("ONEPORT_RUNTIME_CONFIG", str(runtime))
    loaded = load_platform()
    registry, journal = loaded.registry, loaded.store
    token = operator_token_from_environment().get_secret_value()
    adapter = registry.get("gcp-test")
    assert isinstance(adapter, DeploymentProvider)
    assert token == TOKEN
    spec = DeploymentSpec.model_validate(h.request.model_dump(mode="json"))
    service = DeploymentService(registry, journal)
    assert service.plan(spec).steps and not h.api.writes
    status = service.submit(spec, "native-common-sdk-test-key")
    assert status.status == "queued" and status.actions == []
    assert not h.api.writes
    env = adapter.public_environment()
    assert env["input_kind"] == "image" and env["configurable_rollout"]
    assert adapter.capabilities().features["redis"].stage == "unsupported"


@pytest.fixture
def sdk():
    settings = GcpSettings(
        environment={
            "application": "demo",
            "environment_id": "gcp-test",
            "project_id": "test-project",
            "region": "asia-northeast3",
            "service": "demo",
            "service_uid": "registered-uid",
            "service_url": "https://demo.gcp.example.com",
        },
        database_url="postgresql://unused",
        allowed_images=[IMAGE],
        poll_seconds=1,
        required_health_samples=2,
        production_check_limit=4,
    )
    api = CloudRunApi(settings.environment, OLD_IMAGE)
    provider = CloudRunProvider(api, api, settings.environment)
    request = GcpDeploymentRequest(
        application="demo",
        environment_id="gcp-test",
        source={"kind": "image", "image": IMAGE},
        strategy="canary",
        rollout={
            "canary_percent": 17,
            "hold_seconds": 30,
            "promotion": "automatic",
            "bake_seconds": 15,
        },
    )
    return SimpleNamespace(settings=settings, api=api, provider=provider, request=request)


@pytest.fixture
def harness(sdk, store):
    sdk.settings.database_url = SecretStr(store.dsn)
    runtime = GcpRuntime(sdk.settings)
    runtime.provider = sdk.provider
    runtime.connect = Mock()
    runtime.probe = Mock(return_value=True)
    sdk.clock = 1000
    runtime.now = lambda: sdk.clock
    sdk.store, sdk.runtime = store, runtime
    sdk.worker = GcpWorker(store, runtime)
    return sdk


def enqueue(h):
    return h.store.enqueue(
        h.request,
        "gcp-test-request-0001",
        h.settings.fingerprint(),
        runtime=Runtime.GCP_CLOUD_RUN,
        timeout_seconds=h.settings.release_timeout_seconds,
    )


def test_public_image_and_plain_env_preserve_cloud_run_secrets(sdk):
    from controlplane.domain.models import ImageSource

    image = "docker.io/team/app@sha256:" + "c" * 64
    request = sdk.request.model_copy(
        update={"source": ImageSource(image=image), "env": {"VERSION": "public-demo"}}
    )
    sdk.settings.allowed_images = [image]
    sdk.settings.validate_request(request)
    template = sdk.provider.candidate_template(sdk.api.service, request, "demo-next", "a" * 24)
    container = template.containers[0]
    assert container.image == image
    values = {e.name: e for e in container.env}
    assert values["VERSION"].value == "public-demo"
    assert values["DB_PASSWORD"].value_source.secret_key_ref.secret == "app-db"
    bad = request.model_copy(update={"env": {"DB_PASSWORD": "plaintext"}})
    with pytest.raises(ProviderConflict, match="Secret"):
        sdk.provider.candidate_template(sdk.api.service, bad, "demo-next", "a" * 24)


def tick(h):
    with h.store.connect() as conn:
        conn.execute("UPDATE releases SET next_attempt_at=now()")
    h.worker.tick()


def test_public_image_worker_verifies_before_cloud_run_writes(harness, monkeypatch):
    from controlplane.domain.models import ImageSource

    h = harness
    image = "docker.io/team/app@sha256:" + "c" * 64
    h.settings.allowed_images = [image]
    h.request = h.request.model_copy(
        update={"source": ImageSource(image=image), "strategy": Strategy.BLUE_GREEN}
    )
    evidence = {"image": image, "platform": "linux/amd64", "access": "public"}
    verify = Mock(return_value=evidence)
    monkeypatch.setattr("controlplane.execution.image_preflight.verify_public_image", verify)
    row = enqueue(h)
    tick(h)
    assert not h.api.writes
    assert h.store.get(row["id"])["checkpoint"]["image_verification"] == evidence
    advance(h, row, {"baking"})
    h.clock += 15
    result = advance(h, row, {"succeeded"})
    assert result["state"] == "succeeded"
    assert h.api.service.template.containers[0].image == image
    verify.assert_called_once_with(image, "X86_64")


def advance(h, row, states, limit=70):
    for _ in range(limit):
        actual = h.store.get(row["id"])
        if actual["state"] in states:
            return actual
        if actual["state"] in TERMINAL:
            pytest.fail(f"Unexpected terminal state {actual['state']}: {actual['error_code']}")
        tick(h)
    pytest.fail(f"State did not reach {states}: {h.store.get(row['id'])['state']}")


def action(h, row, value):
    return h.store.request_action(
        row["id"],
        value,
        application="demo",
        environment_id="gcp-test",
        binding_fingerprint=h.settings.fingerprint(),
    )


def test_candidate_preserves_network_identity_secrets_and_pins_rollback(sdk):
    cp = sdk.provider.snapshot(sdk.request, "c" * 24)
    before = run_v2.Service(sdk.api.service)
    sdk.provider.submit(sdk.request, "c" * 24, cp)
    after = sdk.api.service
    assert after.ingress == before.ingress
    assert after.template.service_account == before.template.service_account
    assert after.template.vpc_access == before.template.vpc_access
    assert after.template.containers[0].env[:2] == before.template.containers[0].env
    assert after.template.containers[0].image == IMAGE
    assert weights(after) == {"demo-v1": 100}
    assert cp["original_traffic"][0]["type_"] != cp["rollback_traffic"][0]["type_"]
    assert cp["rollback_traffic"][0]["revision"] == "demo-v1"
    sdk.provider.submit(sdk.request, "c" * 24, cp)
    assert len(sdk.api.writes) == 1


def test_accepted_write_timeout_is_reconciled_without_duplicate_candidate(sdk):
    cp = sdk.provider.snapshot(sdk.request, "c" * 24)
    sdk.api.fail_after_write = True
    with pytest.raises(SubmissionUncertain):
        sdk.provider.submit(sdk.request, "c" * 24, cp)
    sdk.provider.submit(sdk.request, "c" * 24, cp)
    assert len(sdk.api.writes) == 1


@pytest.mark.parametrize("mutation", ["uid", "template", "traffic"])
def test_foreign_changes_prevent_mutation(sdk, mutation):
    cp = sdk.provider.snapshot(sdk.request, "c" * 24)
    if mutation == "uid":
        sdk.api.service.uid = "replacement"
    elif mutation == "template":
        sdk.api.service.template.containers[0].image = OLD_IMAGE + "foreign"
    else:
        sdk.api.service.traffic[0].percent = 80
    with pytest.raises(ProviderConflict):
        sdk.provider.submit(sdk.request, "c" * 24, cp)
    assert not sdk.api.writes


def test_automatic_canary_uses_configured_percent_and_durable_clock(harness):
    h = harness
    row = enqueue(h)
    holding = advance(h, row, {"holding"})
    candidate = holding["checkpoint"]["candidate"]
    assert weights(h.api.service) == {"demo-v1": 83, candidate: 17}
    # A restart must not reset the persisted timer or advance early.
    h.worker = GcpWorker(h.store, h.runtime)
    h.clock += 29
    tick(h)
    assert h.store.get(row["id"])["state"] == "holding"
    h.clock += 1
    advance(h, row, {"baking"})
    assert weights(h.api.service) == {candidate: 100}
    h.clock += 15
    assert advance(h, row, TERMINAL)["state"] == "succeeded"


@pytest.mark.parametrize("strategy", ["canary", "blue_green"])
def test_manual_promotion_waits_for_operator_and_canary_hold(harness, strategy):
    h = harness
    h.request.strategy = Strategy(strategy)
    h.request.rollout.promotion = "manual"
    row = enqueue(h)
    if strategy == "canary":
        advance(h, row, {"holding"})
        with pytest.raises(ValueError):
            action(h, row, "promote")
        h.clock += 30
    waiting = advance(h, row, {"awaiting_promotion"})
    assert weights(h.api.service)["demo-v1"] == (83 if strategy == "canary" else 100)
    for _ in range(3):
        tick(h)
    assert h.store.get(row["id"])["state"] == "awaiting_promotion"
    action(h, waiting, "promote")
    advance(h, row, {"baking"})
    h.clock += 15
    assert advance(h, row, TERMINAL)["state"] == "succeeded"


def test_blue_green_automatic_skips_partial_traffic(harness):
    h = harness
    h.request.strategy = Strategy.BLUE_GREEN
    row = enqueue(h)
    advance(h, row, {"baking"})
    assert all(t.percent in (0, 100) for _, s in h.api.writes for t in s.traffic)
    h.clock += 15
    assert advance(h, row, TERMINAL)["state"] == "succeeded"


@pytest.mark.parametrize("phase", ["holding", "baking", "verifying"])
def test_auto_rollback_restores_original_revision_even_when_original_target_was_latest(
    harness, phase
):
    h = harness
    row = enqueue(h)
    if phase == "baking":
        advance(h, row, {"holding"})
        h.clock += 30
    current = advance(h, row, {phase})
    candidate_id = row["id"]
    h.runtime.probe.side_effect = lambda *a, release, **k: release != candidate_id
    result = advance(h, row, TERMINAL)
    assert result["state"] == "failed"
    assert result["error_code"].endswith("_restored")
    assert weights(h.api.service) == {"demo-v1": 100}
    assert h.api.service.traffic[0].type_ == REVISION
    assert current["checkpoint"]["stable"] == "demo-v1"


def test_automatic_rollback_off_leaves_operator_in_control(harness):
    h = harness
    h.request.rollout.auto_rollback = False
    h.request.rollout.promotion = "manual"
    row = enqueue(h)
    advance(h, row, {"holding"})
    h.runtime.probe.side_effect = lambda *a, release, **k: release != row["id"]
    h.clock += 30
    waiting = advance(h, row, {"awaiting_promotion"})
    assert weights(h.api.service)["demo-v1"] == 83
    action(h, waiting, "rollback")
    assert advance(h, row, TERMINAL)["error_code"] == "operator_rollback_restored"


def test_rollback_disabled_failure_after_full_switch_waits_for_manual_recovery(harness):
    h = harness
    h.request.rollout.auto_rollback = False
    h.request.strategy = Strategy.BLUE_GREEN
    row = enqueue(h)
    advance(h, row, {"baking"})
    h.runtime.probe.side_effect = lambda *a, release, **k: release != row["id"]
    waiting = advance(h, row, {"awaiting_rollback"})
    assert "demo-v1" not in weights(h.api.service)
    action(h, waiting, "rollback")
    assert advance(h, row, TERMINAL)["error_code"] == "operator_rollback_restored"


def test_traffic_timer_starts_only_after_actual_convergence(harness):
    h = harness
    row = enqueue(h)
    advance(h, row, {"verifying"})
    h.api.delay_traffic = True
    waiting = advance(h, row, {"traffic_waiting"})
    h.clock += 300
    tick(h)
    assert h.store.get(row["id"])["state"] == "traffic_waiting"
    h.api.settle()
    holding = advance(h, row, {"holding"})
    assert holding["checkpoint"]["until"] == h.clock + 30
    assert waiting["checkpoint"]["next"] == "holding"


def test_worker_recovers_accepted_traffic_write_without_replaying_it(harness):
    h = harness
    row = enqueue(h)
    advance(h, row, {"switching"})
    h.api.fail_after_write = True
    tick(h)
    assert h.store.get(row["id"])["state"] == "switching"
    count = len(h.api.writes)
    advance(h, row, {"holding"})
    assert len(h.api.writes) == count


def test_foreign_traffic_during_hold_is_not_overwritten(harness):
    h = harness
    row = enqueue(h)
    advance(h, row, {"holding"})
    count = len(h.api.writes)
    h.api.service.traffic = [run_v2.TrafficTarget(type_=LATEST, percent=100)]
    assert advance(h, row, TERMINAL)["state"] == "uncertain"
    assert len(h.api.writes) == count


def test_foreign_tags_before_submission_fail_without_mutation_or_lock(harness):
    h = harness
    h.api.service.traffic[0].tag = "legacy-controller"
    row = enqueue(h)
    finished = advance(h, row, TERMINAL)
    assert finished["state"] == "failed"
    assert finished["error_code"] == "cloud_run_preflight_conflict"
    assert not h.api.writes
    retry = h.store.enqueue(
        h.request,
        "gcp-preflight-retry-key",
        h.settings.fingerprint(),
        runtime=Runtime.GCP_CLOUD_RUN,
        timeout_seconds=h.settings.release_timeout_seconds,
    )
    assert retry["id"] != row["id"] and retry["state"] == "queued"


def test_deadline_in_manual_wait_rolls_back_when_enabled(harness):
    h = harness
    h.request.rollout.promotion = "manual"
    row = enqueue(h)
    advance(h, row, {"holding"})
    with h.store.connect() as conn:
        conn.execute("UPDATE releases SET deadline=now()-interval '1 second'")
    assert advance(h, row, TERMINAL)["error_code"] == "deadline_exceeded_restored"


def test_deadline_rollback_takes_precedence_over_pending_promotion(harness):
    h = harness
    h.request.rollout.promotion = "manual"
    h.request.strategy = Strategy.BLUE_GREEN
    row = enqueue(h)
    advance(h, row, {"awaiting_promotion"})
    action(h, row, "promote")
    with h.store.connect() as conn:
        conn.execute("UPDATE releases SET deadline=now()-interval '1 second'")
    assert advance(h, row, TERMINAL)["error_code"] == "deadline_exceeded_restored"
    assert weights(h.api.service) == {"demo-v1": 100}


@pytest.mark.parametrize(
    "policy",
    [
        {"canary_percent": 0},
        {"canary_percent": 100},
        {"canary_percent": 10.5},
        {"hold_seconds": -1},
        {"hold_seconds": 3601},
        {"promotion": "unknown"},
        {"failure_threshold": 0},
        {"bake_seconds": 0},
    ],
)
def test_invalid_policy_rejected(policy):
    with pytest.raises(ValidationError):
        RolloutPolicy(**policy)


def test_onprem_rejects_gcp_rollout_instead_of_silently_ignoring_it(sdk):
    with pytest.raises(ValidationError):
        ArgoDeploymentRequest.model_validate(sdk.request.model_dump(mode="json"))


def test_http_probe_checks_status_revision_header_and_no_redirects(sdk):
    rt = GcpRuntime(sdk.settings)
    replies = [(200, "wrong"), (503, "release"), (302, "release"), (200, "release")]

    def respond(request):
        status, release = replies.pop(0)
        return httpx.Response(status, headers={"X-Launchpad-Release": release})

    rt.http = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False)
    for expected in (False, False, False, True):
        assert (
            rt.probe(
                sdk.request,
                sdk.api.service,
                url=sdk.settings.environment.service_url,
                release="release",
            )
            is expected
        )
    with pytest.raises(ProviderConflict):
        rt.probe(sdk.request, sdk.api.service, url="https://unregistered.example", release=None)
    rt.http.close()


def test_api_configurable_canary_action_auth_ownership_and_journal(harness):
    h = harness
    h.request.rollout.promotion = "manual"
    registry = ProviderRegistry()
    registry.register(GcpAdapter(h.settings, store=h.store))
    with TestClient(create_app(registry=registry, store=h.store, operator_token=TOKEN)) as client:
        caps = {c["runtime"]: c for c in client.get("/api/v1/providers").json()}
        assert caps["gcp_cloud_run"]["strategies"] == ["blue_green", "canary"]
        body = {
            "idempotency_key": "gcp-http-request-0001",
            "request": h.request.model_dump(mode="json"),
        }
        plan = client.post("/api/v1/plans", json=body["request"])
        assert plan.status_code == 200
        assert "17%" in plan.json()["steps"][-2]["detail"]
        response = client.post("/api/v1/releases", headers=HEADERS, json=body)
        assert response.status_code == 202
        row = response.json()
        assert not h.api.writes
        assert client.post("/api/v1/releases", headers=HEADERS, json=body).json()["id"] == row["id"]
        changed = {
            **body,
            "request": {
                **body["request"],
                "rollout": {**body["request"]["rollout"], "canary_percent": 25},
            },
        }
        assert client.post("/api/v1/releases", headers=HEADERS, json=changed).status_code == 409
        persisted = h.store.get(row["id"])
        assert (persisted["deadline"] - persisted["created_at"]).total_seconds() == 7200
        path = f"/api/v1/releases/{row['id']}/actions"
        assert client.post(path, json={"action": "promote"}).status_code == 401
        assert client.post(path, headers=HEADERS, json={"action": "promote"}).status_code == 409
        advance(h, row, {"holding"})
        h.clock += 30
        advance(h, row, {"awaiting_promotion"})
        detail = client.get(f"/api/v1/releases/{row['id']}", headers=HEADERS).json()
        assert detail["actions"] == ["promote", "rollback"]
        assert client.post(path, headers=HEADERS, json={"action": "promote"}).status_code == 202
        assert client.post(path, headers=HEADERS, json={"action": "rollback"}).status_code == 409
        advance(h, row, {"baking"})
        h.clock += 15
        advance(h, row, TERMINAL)
        assert client.post(path, headers=HEADERS, json={"action": "rollback"}).status_code == 409
        assert (
            client.post(
                "/api/v1/releases/unknown/actions", headers=HEADERS, json={"action": "rollback"}
            ).status_code
            == 404
        )


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/_ah/health"])
def test_reserved_public_health_paths_rejected_before_enqueue(sdk, path):
    value = sdk.request.model_dump(mode="json")
    value["health_path"] = path
    with pytest.raises(ValueError, match="Cloud Run reserves"):
        GcpDeploymentRequest.model_validate(value)
    assert not sdk.api.writes


def test_native_health_default_and_explicit_ready(sdk):
    value = sdk.request.model_dump(mode="json")
    value.pop("health_path", None)
    assert GcpDeploymentRequest.model_validate(value).health_path == "/health"
    value["health_path"] = "/ready"
    assert GcpDeploymentRequest.model_validate(value).health_path == "/ready"


@pytest.mark.parametrize("protocol", ["", "http1", "h2c"])
def test_cloud_run_port_defaulting_does_not_change_template_fingerprint(sdk, protocol):
    from controlplane.providers.gcp.provider import digest

    sdk.api.service.template.containers[0].ports = [
        run_v2.ContainerPort(name=protocol, container_port=8080)
    ]
    wanted = sdk.provider.candidate_template(sdk.api.service, sdk.request, "demo-next", "a" * 24)
    observed = run_v2.RevisionTemplate(wanted)
    port = observed.containers[0].ports[0]
    if not port.name:
        port.name = "http1"  # Cloud Run API response normalization.
    assert port.name == (protocol or "http1")
    assert digest(observed) == digest(wanted)
