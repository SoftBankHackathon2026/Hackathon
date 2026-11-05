"""Real PostgreSQL API intake wired to the migrated worker; provider transport is a double."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from test_argo import TERMINAL, advance
from test_argo import harness as harness
from test_argo import sdk as sdk

from controlplane.core.config import HttpSettings
from controlplane.domain.models import Runtime
from controlplane.main import create_app
from controlplane.providers.interface import DeploymentProvider
from controlplane.providers.onprem.adapter import OnpremAdapter
from controlplane.providers.registry import ProviderRegistry

TOKEN = "oneport-test-operator-token-0123456789"
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def client(harness):
    registry = ProviderRegistry()
    registry.register(OnpremAdapter(harness.runtime.settings))
    with TestClient(create_app(registry=registry, store=harness.store, operator_token=TOKEN)) as c:
        yield c


def body(h, key="api-idempotency-key-0001"):
    return {"request": h.request.model_dump(mode="json"), "idempotency_key": key}


def test_preview_boots_without_db_credentials_or_cloud_sdks(monkeypatch):
    monkeypatch.delenv("ONEPORT_RUNTIME_CONFIG", raising=False)
    with TestClient(create_app()) as c:
        assert c.get("/healthz").json()["execution_configured"] is False
        assert all(not e["executable"] for e in c.get("/api/v1/environments").json())
        assert c.post("/api/v1/releases", json={}).status_code == 503
        assert c.get("/api/v1/releases").status_code == 503
        caps = {v["runtime"]: v for v in c.get("/api/v1/providers").json()}
        assert caps["aws_ecs"]["integration"] == "sdk_only"
        assert caps["gcp_cloud_run"]["strategies"] == []
        assert caps["onprem_argo"]["integration"] == "sdk_only"


def test_environment_list_reads_each_visible_binding_once_and_skips_hidden_metadata():
    registry = ProviderRegistry()
    visible = Mock(spec=DeploymentProvider)
    visible.environment_id = "visible-aws"
    visible.runtime = Runtime.AWS_ECS
    visible.capabilities.return_value.runtime = visible.runtime
    visible.public_environment.return_value = {
        "id": "visible-aws",
        "runtime": Runtime.AWS_ECS,
        "executable": True,
    }
    hidden = Mock(spec=DeploymentProvider)
    hidden.environment_id = "hidden-gcp"
    hidden.runtime = Runtime.GCP_CLOUD_RUN
    hidden.capabilities.return_value.runtime = hidden.runtime
    hidden.public_environment.side_effect = AssertionError("Hidden state must not be loaded")
    registry.register(visible)
    registry.register(hidden, listed=False)
    with TestClient(create_app(registry=registry)) as c:
        response = c.get("/api/v1/environments")
    assert response.status_code == 200
    assert {e["id"] for e in response.json()} == {"visible-aws", "onprem-helm-unregistered"}
    visible.public_environment.assert_called_once()
    hidden.public_environment.assert_not_called()


def test_auth_and_origin_are_checked_before_queueing(client, harness):
    assert client.post("/api/v1/releases", json=body(harness)).status_code == 401
    assert (
        client.post(
            "/api/v1/releases",
            json=body(harness),
            headers={**HEADERS, "Origin": "https://untrusted.example"},
        ).status_code
        == 403
    )
    assert harness.store.list() == []


def test_public_host_and_cors_still_require_token(harness):
    registry = ProviderRegistry()
    registry.register(OnpremAdapter(harness.runtime.settings))
    settings = HttpSettings(
        allowed_hosts=["platform.example.com"], allowed_origins=["https://platform.example.com"]
    )
    app = create_app(
        registry=registry, store=harness.store, operator_token=TOKEN, http_settings=settings
    )
    with TestClient(app, base_url="https://platform.example.com") as c:
        origin = {"Origin": "https://platform.example.com"}
        preflight = c.options(
            "/api/v1/releases",
            headers={
                **origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == origin["Origin"]
        assert c.get("/api/v1/releases", headers=origin).status_code == 401
        assert c.get("/api/v1/releases", headers={**origin, **HEADERS}).status_code == 200
        assert (
            c.get(
                "/api/v1/releases", headers={**HEADERS, "Origin": "https://evil.example.com"}
            ).status_code
            == 403
        )
        assert c.get("/healthz", headers={"Host": "evil.example.com"}).status_code == 400


@pytest.mark.parametrize(
    "fields",
    [
        {"allowed_hosts": ["*"]},
        {"allowed_hosts": ["https://example.com"]},
        {"allowed_origins": ["*"]},
        {"allowed_origins": ["https://example.com/path"]},
    ],
)
def test_wildcard_or_malformed_public_access_rejected(fields):
    with pytest.raises(ValueError):
        HttpSettings(**fields)


def test_api_enqueue_idempotency_worker_and_query_same_release(client, harness):
    h = harness
    response = client.post("/api/v1/releases", json=body(h), headers=HEADERS)
    assert response.status_code == 202
    row = response.json()
    assert row["status"] == row["phase"] == "queued"
    assert row["runtime"] == "onprem_argo"
    # No Kubernetes client is connected by API intake.
    h.runtime.connect.assert_not_called()
    again = client.post("/api/v1/releases", json=body(h), headers=HEADERS)
    assert again.json()["id"] == row["id"]
    changed = body(h)
    changed["request"]["port"] = 9090
    assert client.post("/api/v1/releases", json=changed, headers=HEADERS).status_code == 409
    assert advance(h, row, TERMINAL)["state"] == "succeeded"
    detail = client.get("/api/v1/releases/" + row["id"], headers=HEADERS).json()
    assert detail["status"] == "succeeded" and detail["events"]
    assert detail["service_url"] == "https://demo-a1b2.example.com"
    assert (
        "checkpoint" not in detail
        and "request" not in detail
        and "binding_fingerprint" not in detail
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"environment_id": "aws-unknown"},
        {"environment_id": []},
        {"application": "other-app"},
        {"cpu": "2"},
        {"replicas": 5},
        {"source": {"kind": "image", "image": "registry.example/app:latest"}},
        {"source": {"kind": "image", "image": "registry.example/app@sha256:" + "b" * 64}},
        {"env": {"PORT": "9000"}},
        {"credentials": "do-not-echo-secret"},
    ],
)
def test_bad_or_unregistered_requests_never_enqueue(client, harness, mutation):
    value = body(harness)
    value["request"].update(mutation)
    response = client.post("/api/v1/releases", json=value, headers=HEADERS)
    assert response.status_code in {409, 422}
    assert "do-not-echo-secret" not in response.text
    assert harness.store.list() == []


def test_history_filters_other_provider_rows(client, harness):
    h = harness
    foreign = h.store.enqueue(h.request, "another-provider-key", "foreign", runtime=Runtime.AWS_ECS)
    assert client.get("/api/v1/releases", headers=HEADERS).json() == []
    assert client.get("/api/v1/releases/" + foreign["id"], headers=HEADERS).status_code == 404


def test_environment_does_not_disclose_credentials_or_claim_connectivity(client):
    env = next(e for e in client.get("/api/v1/environments").json() if e["executable"])
    assert env["connection_verified"] is False
    assert not {"database_url", "kubeconfig", "context", "server", "operator_token"} & env.keys()


def test_registry_rejects_conflicting_environment_ownership(harness):
    r = ProviderRegistry()
    adapter = OnpremAdapter(harness.runtime.settings)
    r.register(adapter)
    with pytest.raises(ValueError):
        r.register(adapter)


def test_plan_changes_with_request_without_queueing(client, harness):
    payload = harness.request.model_dump(mode="json", exclude={"env"})
    first = client.post("/api/v1/plans", json=payload)
    assert first.status_code == 200
    payload["port"] = 9090
    second = client.post("/api/v1/plans", json=payload)
    assert second.status_code == 200
    assert first.json()["fingerprint"] != second.json()["fingerprint"]
    assert [s["id"] for s in first.json()["steps"]] == ["validate", "deploy", "verify", "promote"]
    assert harness.store.list() == []


def test_registry_uses_registered_provider_capabilities():
    from unittest.mock import Mock

    from controlplane.providers.capabilities import ProviderCapabilities
    from controlplane.providers.interface import DeploymentProvider

    provider = Mock(spec=DeploymentProvider)
    provider.runtime = Runtime.GCP_CLOUD_RUN
    provider.application = "team-app"
    provider.environment_id = "gcp-team-dev"
    provider.capabilities.return_value = ProviderCapabilities(
        runtime=provider.runtime, integration="sdk_only", strategies=["blue_green"]
    )
    registry = ProviderRegistry()
    registry.register(provider)
    with TestClient(create_app(registry=registry)) as client:
        caps = {c["runtime"]: c for c in client.get("/api/v1/providers").json()}
        assert caps["gcp_cloud_run"]["integration"] == "sdk_only"
        assert caps["gcp_cloud_run"]["strategies"] == ["blue_green"]


def test_registry_rejects_metadata_only_adapter():
    from types import SimpleNamespace

    with pytest.raises(TypeError, match="common deployment interface"):
        ProviderRegistry().register(SimpleNamespace(runtime=Runtime.AWS_ECS))


def test_preview_environment_ids_follow_common_spec(monkeypatch):
    from controlplane.providers.interface import DeploymentSpec

    monkeypatch.delenv("ONEPORT_CONFIG", raising=False)
    monkeypatch.delenv("ONEPORT_RUNTIME_CONFIG", raising=False)
    with TestClient(create_app()) as client:
        for environment in client.get("/api/v1/environments").json():
            spec = DeploymentSpec(
                application="demo",
                environment_id=environment["id"],
                source={"kind": "image", "image": "registry.example/app@sha256:" + "a" * 64},
            )
            response = client.post("/api/v1/plans", json=spec.requested())
            assert response.status_code == 409  # Registration missing, not malformed ID.


def test_demo_without_token_accepts_browser_request_but_keeps_guards(harness):
    registry = ProviderRegistry()
    registry.register(OnpremAdapter(harness.runtime.settings))
    app = create_app(registry=registry, store=harness.store, auth_mode="demo")
    with TestClient(app) as c:
        assert c.get("/api/v1/access").json()["auth_mode"] == "demo"
        assert c.get("/api/v1/releases").status_code == 200
        assert c.post("/api/v1/releases", json=body(harness)).status_code == 403
        assert (
            c.post(
                "/api/v1/releases", json=body(harness), headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        origin = {"Origin": "http://127.0.0.1:4320"}
        invalid = body(harness)
        invalid["request"]["replicas"] = 99
        assert c.post("/api/v1/releases", json=invalid, headers=origin).status_code == 422
        assert harness.store.list() == []
        response = c.post("/api/v1/releases", json=body(harness), headers=origin)
        assert response.status_code == 202
        assert advance(harness, response.json(), TERMINAL)["state"] == "succeeded"
        assert c.get("/healthz").json()["mode"] == "demo"
        assert c.get("/healthz", headers={"Host": "evil.example"}).status_code == 400


def test_auth_mode_is_explicit_and_environment_overrides_packaged_setting(tmp_path, monkeypatch):
    from controlplane.core.config import deployment_auth_mode

    monkeypatch.delenv("ONEPORT_AUTH_MODE", raising=False)
    path = tmp_path / "deployment-mode.json"
    assert deployment_auth_mode(path) == "operator"
    path.write_text('{"auth_mode":"demo"}')
    assert deployment_auth_mode(path) == "demo"
    monkeypatch.setenv("ONEPORT_AUTH_MODE", "operator")
    assert deployment_auth_mode(path) == "operator"
    monkeypatch.setenv("ONEPORT_AUTH_MODE", "oops")
    with pytest.raises(ValueError):
        deployment_auth_mode(path)


def test_app_logs_are_refused_for_providers_without_a_log_reader(client):
    found = client.get("/api/v1/environments/test/logs", headers=HEADERS)
    assert found.status_code == 409


def test_traffic_and_traces_are_refused_for_providers_without_telemetry(client):
    for path in ("traffic", "traces", "traces/1-66000010-" + "b" * 24):
        found = client.get(f"/api/v1/environments/test/{path}", headers=HEADERS)
        assert found.status_code == 409


def test_unlisted_environment_preserves_history_without_unregistered_placeholder(harness):
    registry = ProviderRegistry()
    adapter = OnpremAdapter(harness.runtime.settings)
    registry.register(adapter, listed=False)
    assert registry.get(adapter.environment_id) is adapter
    with TestClient(create_app(registry=registry, store=harness.store, operator_token=TOKEN)) as c:
        response = c.post("/api/v1/releases", json=body(harness), headers=HEADERS)
        assert response.status_code == 202
        environments = c.get("/api/v1/environments").json()
        assert all(e["runtime"] != "onprem_argo" for e in environments)
        # Listing is discovery only: the retained adapter can still serve old records.
        assert c.get("/api/v1/releases", headers=HEADERS).json()
