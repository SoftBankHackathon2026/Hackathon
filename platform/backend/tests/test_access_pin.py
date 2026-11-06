import pytest
from fastapi.testclient import TestClient

from controlplane.api import access_pin
from controlplane.api.access_pin import MAX_FAILURES, PinGate, gate_from_environment
from controlplane.main import create_app

BASE = "https://testserver"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ONEPORT_ACCESS_PIN", "4821")
    return TestClient(create_app(auth_mode="demo"), base_url=BASE)


def test_without_a_pin_nothing_is_gated(monkeypatch):
    monkeypatch.delenv("ONEPORT_ACCESS_PIN", raising=False)
    client = TestClient(create_app(auth_mode="demo"), base_url=BASE)
    assert client.get("/api/v1/providers").status_code == 200
    assert client.get("/api/v1/access-pin").json() == {"required": False, "unlocked": True}


def test_api_is_closed_until_the_pin_is_entered(client):
    blocked = client.get("/api/v1/providers")
    assert blocked.status_code == 401
    assert blocked.json()["pin_required"] is True
    assert client.get("/api/v1/access-pin").json() == {"required": True, "unlocked": False}
    assert client.get("/healthz").status_code == 200

    assert client.post("/api/v1/access-pin", json={"pin": "0000"}).status_code == 401
    unlocked = client.post("/api/v1/access-pin", json={"pin": "4821"})
    assert unlocked.status_code == 200
    cookie = unlocked.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "Path=/api" in cookie

    assert client.get("/api/v1/providers").status_code == 200
    assert client.get("/api/v1/access-pin").json() == {"required": True, "unlocked": True}


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/ingest/migrations/forward_001",
        "/api/v1/enrollments/scripts/oneport-enroll-k8s.sh",
        "/api/v1/enrollments/abc/complete",
    ],
)
def test_machine_endpoints_keep_their_own_auth(client, path):
    method = client.get if "scripts" in path else client.post
    assert "pin_required" not in method(path).text


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/enrollments",
        "/api/v1/enrollments/abc",
        "/api/v1/enrollments/abc/completed",
        "/api/v1/access-pin/x",
    ],
)
def test_only_the_listed_machine_paths_are_open(client, path):
    assert client.get(path).json().get("pin_required") is True


def test_forged_or_expired_cookies_are_rejected():
    gate = PinGate("4821")
    now = 1_000_000.0
    token = gate.issue(now)
    assert gate.valid(token, now)
    expires, _, signature = token.partition(".")
    assert not gate.valid(f"{int(expires) + 999}.{signature}", now)
    assert not gate.valid(token, int(expires) + 1)
    assert not PinGate("4821").valid(token, now)
    assert not gate.valid("", now) and not gate.valid(None, now) and not gate.valid("x.y", now)


def test_repeated_failures_lock_even_the_right_pin():
    gate = PinGate("4821")
    for _ in range(MAX_FAILURES):
        assert not gate.attempt("1111", 0.0)
    assert gate.locked(1.0)
    assert not gate.attempt("4821", 1.0)
    assert gate.attempt("4821", access_pin.FAILURE_WINDOW_SECONDS + 1.0)


def test_locked_gate_answers_429(client):
    for _ in range(MAX_FAILURES):
        client.post("/api/v1/access-pin", json={"pin": "1111"})
    assert client.post("/api/v1/access-pin", json={"pin": "4821"}).status_code == 429


@pytest.mark.parametrize("pin", ["123", "12345", "abcd", "12 4"])
def test_pin_must_be_four_digits(monkeypatch, pin):
    monkeypatch.setenv("ONEPORT_ACCESS_PIN", pin)
    with pytest.raises(ValueError):
        gate_from_environment()
