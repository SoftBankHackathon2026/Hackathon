"""HTTP authorization and parsing boundaries without live customer resources."""

from types import SimpleNamespace

import pytest
from controlplane.api.deps import get_environments, optional_viewer
from controlplane.api.errors import register_exception_handlers
from controlplane.api.routes import connections, environments
from controlplane.core.config import AuthMode
from controlplane.schemas.accounts import Identity
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_control import (
    DEVICE,
    OWNER,
    PUBLIC,
    approved,
    envelope,
    issued,
    proof,
    ready,
    report,
)
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


def test_confirmation_accepts_hyphen_whitespace_and_lowercase(client, svc):
    request_id = issued(svc)
    proved = svc.prove(request_id, proof(svc, request_id))
    code = proved["confirmation_code"].lower()
    response = client.post(
        f"/api/v1/connections/{request_id}/approve",
        json={
            "candidateDigest": proved["candidate_digest"],
            "code": f" {code[:4]}-\t{code[4:]} ",
        },
    )
    assert response.status_code == 200
    assert response.json()["device"] == "APPROVED"
    assert response.json()["attemptsLeft"] == 5
    assert svc.limiter.failures == 0


def test_request_block_cannot_be_evaded_by_forwarded_headers_or_block_other_requests(
    client, svc
):
    first = issued(svc)
    for n in range(20):
        body = proof(svc, first)
        body["device_signature"] = "invalid"
        response = client.post(
            f"/api/v1/connections/{first}/prove",
            json=body,
            headers={"X-Forwarded-For": f"198.51.100.{n + 1}"},
        )
        assert response.status_code == 401
    assert (
        client.post(
            f"/api/v1/connections/{first}/prove",
            json=body,
            headers={"X-Forwarded-For": "203.0.113.1"},
        ).json()["code"]
        == "AUTHENTICATION_BLOCKED"
    )
    created = client.post(
        "/api/v1/connections",
        json={"publicKey": PUBLIC, "operationId": "another_operation_123"},
    )
    assert created.status_code == 201
    second = created.json()["id"]
    assert (
        client.post(
            f"/api/v1/connections/{second}/prove", json=proof(svc, second)
        ).status_code
        == 200
    )


def test_legacy_environment_list_is_independent_of_connection_storage():
    class BrokenConnections:
        def environments(self, actor):
            raise KeyError("target")

    identity = Identity(email="owner@example.test", team="team", role="owner")
    legacy = [{"id": "legacy", "runtime": "onprem_helm"}]
    app = FastAPI()
    app.state.container = SimpleNamespace(connections=BrokenConnections())
    app.include_router(environments.router)
    app.dependency_overrides[optional_viewer] = lambda: identity
    app.dependency_overrides[get_environments] = lambda: SimpleNamespace(
        overview=lambda _: legacy
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/environments").json() == legacy


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


@pytest.mark.parametrize(
    "finish", ["expired", "rekey", "revoke", "complete", "lease_expired"]
)
def test_collection_pending_snapshot_does_not_expose_signed_task(client, svc, finish):
    request_id = ready(svc)
    path = f"/api/v1/connections/{request_id}"
    assert client.get(path).json()["collectionPending"] is False
    task, value = report(svc, request_id)
    pending = client.get(path).json()
    assert pending["collectionPending"] is True
    assert "task" not in pending and "signature" not in pending
    assert task["job_id"] not in str(pending)
    if finish == "expired":
        svc.test_now[0] += 120000
        svc.tunnel(
            request_id,
            {
                "epoch": 1,
                "connection_id": "connection_1234567890",
                "state": "READY",
                "checks": 2,
                "key_expires_at_ms": svc.store.get(request_id)["key_expires_at"],
                "lease_expires_at_ms": svc.clock() + 60000,
            },
        )
    elif finish == "lease_expired":
        svc.test_now[0] += 60000
    elif finish == "rekey":
        svc.tunnel(
            request_id,
            {
                "epoch": 2,
                "connection_id": "rekey_connection_1234",
                "state": "REKEYING",
            },
        )
    elif finish == "revoke":
        assert client.post(path + "/revoke", json={}).status_code == 200
    else:
        svc.device(
            request_id,
            envelope(
                svc,
                request_id,
                "inventory",
                {"job_id": task["job_id"], "report": value},
            ),
            through_tunnel=True,
        )
    assert client.get(path).json()["collectionPending"] is False


@pytest.fixture
def demo_client(svc):
    app = FastAPI()
    app.state.container = SimpleNamespace(
        connections=svc,
        auth_mode=AuthMode.DEMO,
        http=SimpleNamespace(allowed_origins=["https://platform.example.test"]),
    )
    register_exception_handlers(app)
    app.include_router(connections.router)
    with TestClient(app) as client:
        client.headers["Origin"] = "https://platform.example.test"
        yield client


def test_demo_mode_acts_as_configured_tenant_without_session(demo_client):
    assert demo_client.get("/api/v1/connections/capabilities").json()["available"]
    response = demo_client.post(
        "/api/v1/connections",
        json={"publicKey": PUBLIC, "operationId": "operation_demo_123456"},
    )
    assert response.status_code == 201
    listed = demo_client.get("/api/v1/connections").json()
    assert [item["organization"] for item in listed] == ["team"]
    assert listed[0]["permissions"]["approveDevice"] is True


def test_demo_mode_still_requires_platform_origin(demo_client):
    demo_client.headers["Origin"] = "https://attacker.example.test"
    assert demo_client.post("/api/v1/connections", json={}).status_code == 403
    del demo_client.headers["Origin"]
    assert demo_client.post("/api/v1/connections", json={}).status_code == 403


def test_operator_mode_keeps_connections_closed(svc):
    app = FastAPI()
    app.state.container = SimpleNamespace(
        connections=svc,
        auth_mode=AuthMode.OPERATOR,
        http=SimpleNamespace(allowed_origins=["https://platform.example.test"]),
    )
    register_exception_handlers(app)
    app.include_router(connections.router)
    with TestClient(app) as client:
        client.headers["Origin"] = "https://platform.example.test"
        assert client.get("/api/v1/connections/capabilities").json()["available"] is False
        assert client.get("/api/v1/connections").status_code == 403
