"""No cloud calls: real journal and the teammate GCP HTTP contract."""

import httpx
import pytest
from fastapi.testclient import TestClient

from controlplane.domain.models import Runtime
from controlplane.execution.gcp_bridge.worker import GcpRuntime, GcpWorker
from controlplane.main import create_app
from controlplane.providers.gcp_bridge.adapter import GcpAdapter
from controlplane.providers.gcp_bridge.models import GcpRequest, GcpSettings
from controlplane.providers.registry import ProviderRegistry

REPO = "https://github.com/example/demo"
REMOTE = "a" * 32
IMAGE = "asia-northeast3-docker.pkg.dev/project/apps/demo@sha256:" + "b" * 64


@pytest.fixture
def settings(store):
    return GcpSettings(
        environment_id="gcp-demo",
        database_url=store.dsn,
        allowed_repositories=[REPO],
        service_url="https://demo.example.com",
    )


def enqueue(store, settings):
    request = GcpRequest(environment_id="gcp-demo", source={"repository": REPO})
    return store.enqueue(
        request, "gcp-bridge-test-key-01", settings.fingerprint(), runtime=Runtime.GCP_CLOUD_RUN
    )


def result(**changes):
    return (
        dict(
            deployment_id=REMOTE,
            provider="gcp",
            app_id="demo",
            repository_url=REPO,
            status="SUCCESS",
            image_uri=IMAGE,
            revision="demo-0001",
            health="OK",
            url="https://demo.example.com",
            source_commit="c" * 40,
        )
        | changes
    )


def test_api_to_gcp_worker_and_history(store, settings):
    calls = []

    def upstream(request):
        calls.append(request.method)
        return (
            httpx.Response(202, json={"deployment_id": REMOTE})
            if request.method == "POST"
            else httpx.Response(200, json=result())
        )

    registry = ProviderRegistry()
    registry.register(GcpAdapter(settings))
    token = "gcp-test-operator-token-0123456789012345"
    headers = {"Authorization": "Bearer " + token}
    runtime = GcpRuntime(settings, transport=httpx.MockTransport(upstream))
    try:
        with TestClient(create_app(registry=registry, store=store, operator_token=token)) as client:
            payload = {
                "request": GcpRequest(
                    environment_id="gcp-demo", source={"repository": REPO}
                ).model_dump(mode="json"),
                "idempotency_key": "gcp-test-api-key-001",
            }
            response = client.post("/api/v1/releases", json=payload, headers=headers)
            assert response.status_code == 202
            rid = response.json()["id"]
            assert (
                client.post("/api/v1/releases", json=payload, headers=headers).json()["id"] == rid
            )
            worker = GcpWorker(store, runtime)
            worker.tick()
            worker.tick()
            detail = client.get(f"/api/v1/releases/{rid}", headers=headers).json()
            assert detail["status"] == "succeeded" and detail["image"] == IMAGE
            assert detail["repository"] == REPO and detail["service_access"] == "authenticated"
            assert calls == ["POST", "GET"]
    finally:
        runtime.close()


def test_lost_receipt_is_never_replayed(store, settings):
    calls = []

    def upstream(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("receipt lost")

    row = enqueue(store, settings)
    runtime = GcpRuntime(settings, transport=httpx.MockTransport(upstream))
    try:
        GcpWorker(store, runtime).tick()
        GcpWorker(store, runtime).tick()
        assert store.get(row["id"])["state"] == "uncertain"
        assert calls == ["POST"]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(200, json=result(app_id="other")),
        httpx.Response(200, json=result(url="https://other.example.com")),
    ],
)
def test_lost_or_mismatched_upstream_state_is_not_success(store, settings, response):
    calls = []

    def upstream(request):
        calls.append(request.method)
        return (
            httpx.Response(202, json={"deployment_id": REMOTE})
            if request.method == "POST"
            else response
        )

    row = enqueue(store, settings)
    runtime = GcpRuntime(settings, transport=httpx.MockTransport(upstream))
    try:
        worker = GcpWorker(store, runtime)
        worker.tick()
        worker.tick()
        assert store.get(row["id"])["state"] == "uncertain"
        assert calls == ["POST", "GET"]
    finally:
        runtime.close()


def test_gcp_does_not_silently_ignore_shared_db_configuration(settings):
    with pytest.raises(ValueError):
        GcpAdapter(settings).parse_request(
            {
                "application": "demo",
                "environment_id": "gcp-demo",
                "source": {"repository": REPO},
                "resources": {"database": "postgresql", "database_mode": "shared"},
            }
        )
