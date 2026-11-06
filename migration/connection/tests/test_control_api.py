"""HTTP authorization and parsing boundaries without live customer resources."""

from types import SimpleNamespace

import pytest
from controlplane.api.errors import register_exception_handlers
from controlplane.api.routes import connections
from controlplane.core.config import AuthMode
from controlplane.schemas.accounts import Identity
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_control import DEVICE, OWNER, PUBLIC, approved
from test_control import svc as control_service_fixture

svc = control_service_fixture


@pytest.fixture
def client(svc, monkeypatch):
    app = FastAPI()
    app.state.container = SimpleNamespace(
        connections=svc,
        auth_mode=AuthMode.LOGIN,
        http=SimpleNamespace(allowed_origins=["https://platform.example.test"]),
    )
    register_exception_handlers(app)
    app.include_router(connections.router)
    monkeypatch.setattr(
        connections,
        "session_identity",
        lambda request, container: (
            Identity(email="owner@example.test", team="team", role="owner")
            if request.cookies.get("session") == "valid"
            else None
        ),
    )
    with TestClient(app) as client:
        client.cookies.set("session", "valid")
        client.headers["Origin"] = "https://platform.example.test"
        yield client


def test_capability_and_browser_create(client):
    assert client.get("/api/v1/connections/capabilities").json()["available"]
    response = client.post(
        "/api/v1/connections",
        json={"publicKey": PUBLIC, "operationId": "operation_1234567890"},
    )
    assert response.status_code == 201
    assert len(response.json()["id"]) == 22
    assert client.get("/api/v1/connections").json()[0]["organization"] == "team"


def test_no_session_or_bad_origin_cannot_issue(client):
    client.cookies.clear()
    assert client.post("/api/v1/connections", json={}).status_code == 401
    client.cookies.set("session", "valid")
    client.headers["Origin"] = "https://attacker.example.test"
    assert client.post("/api/v1/connections", json={}).status_code == 403
    del client.headers["Origin"]
    assert client.post("/api/v1/connections", json={}).status_code == 403


@pytest.mark.parametrize(
    "payload",
    [
        '{"publicKey":"x","publicKey":"y","operationId":"z"}',
        '{"publicKey":NaN}',
        '{"publicKey":1.5}',
        "[]",
    ],
)
def test_ambiguous_signed_documents_rejected(client, payload):
    assert client.post("/api/v1/connections", content=payload).status_code == 422


def test_body_limit(client):
    assert (
        client.post(
            "/api/v1/connections", content='{"x":"' + "a" * 32768 + '"}'
        ).status_code
        == 422
    )


def test_unauthenticated_runtime_and_browser_forbidden(client, svc):
    request_id = approved(svc)
    assert (
        client.get(f"/api/v1/connection-runtime/{request_id}/authorize").status_code
        == 401
    )
    response = client.get(
        f"/api/v1/connection-runtime/{request_id}/authorize",
        headers={"Authorization": "Bearer " + svc.runtime_token},
    )
    assert response.status_code == 200
    assert response.json()["device_auth_public_key"] == DEVICE
    assert response.json()["tenant_id"] == OWNER.team


def test_recovery_cannot_tunnel_operation(client, svc):
    request_id = approved(svc)
    response = client.post(
        f"/api/v1/connections/{request_id}/recover",
        json={"payload": {"action": "poll"}, "signature": "x"},
    )
    assert response.status_code == 422


def test_capabilities_disabled_when_optional_package_not_wired(client):
    client.app.state.container.connections = None
    assert client.get("/api/v1/connections/capabilities").json()["available"] is False
    assert client.get("/api/v1/connections").status_code == 503


def test_runtime_health_requires_exact_release(client, svc):
    response = client.post(
        "/api/v1/connection-runtime/health",
        headers={"Authorization": "Bearer " + svc.runtime_token},
        json={
            "release_version": "wrong",
            "protocol_version": 1,
            "crypto_profile": "MLDSA65-X25519MLKEM768-WG",
            "bootstrap_sha384": "a" * 96,
            "tenant_id": "team",
            "gateway_id": "gateway-test",
        },
    )
    assert response.status_code == 422


def test_sensitive_responses_never_cached(client):
    for response in (
        client.get("/api/v1/connections/capabilities"),
        client.post("/api/v1/connections", json={}),
        client.get("/api/v1/connection-runtime/missing/authorize"),
    ):
        assert response.headers["Cache-Control"] == "no-store"


def test_runtime_health_and_auth_failure_contract(client, svc):
    headers = {"Authorization": "Bearer " + svc.runtime_token}
    health = {
        "release_version": svc.release_version,
        "protocol_version": 1,
        "crypto_profile": "MLDSA65-X25519MLKEM768-WG",
        "bootstrap_sha384": svc.bootstrap_sha384,
        "tenant_id": "team",
        "gateway_id": "gateway-test",
    }
    assert (
        client.post(
            "/api/v1/connection-runtime/health", headers=headers, json=health
        ).status_code
        == 200
    )
    health["tenant_id"] = "other"
    assert (
        client.post(
            "/api/v1/connection-runtime/health", headers=headers, json=health
        ).status_code
        == 422
    )
    assert not svc.capabilities("other")["available"]
    body = {"source_ip": "2001:db8::1"}
    assert client.post(
        "/api/v1/connection-runtime/auth-check", headers=headers, json=body
    ).json() == {"allowed": True}
    for _ in range(20):
        assert (
            client.post(
                "/api/v1/connection-runtime/auth-failure", headers=headers, json=body
            ).status_code
            == 200
        )
    assert (
        client.post(
            "/api/v1/connection-runtime/auth-check", headers=headers, json=body
        ).status_code
        == 429
    )


@pytest.mark.parametrize("payload", [None, [], "status"])
def test_malformed_recovery_is_client_error(client, svc, payload):
    request_id = approved(svc)
    assert (
        client.post(
            f"/api/v1/connections/{request_id}/recover",
            json={"payload": payload, "signature": "x"},
        ).status_code
        == 422
    )
