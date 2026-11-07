import os

import pytest
from fastapi.testclient import TestClient

from controlplane.main import create_app
from controlplane.services.traffic import TrafficRoutes

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def routes_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEPORT_INGEST_TOKEN", TOKEN)
    monkeypatch.setenv("ONEPORT_TRAFFIC_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(routes_dir):
    return TestClient(create_app(auth_mode="demo"))


def test_switch_leaves_exactly_one_marker_per_app(client, routes_dir):
    assert client.get("/api/v1/ingest/traffic/sample-app", headers=AUTH).json()["target"] is None
    moved = client.post("/api/v1/ingest/traffic/sample-app", json={"target": "aws"}, headers=AUTH)
    assert moved.status_code == 200 and moved.json()["target"] == "aws"
    assert sorted(p.name for p in routes_dir.iterdir()) == ["sample-app@aws"]

    client.post("/api/v1/ingest/traffic/sample-app", json={"target": "onprem"}, headers=AUTH)
    client.post("/api/v1/ingest/traffic/other-app", json={"target": "aws"}, headers=AUTH)
    assert sorted(p.name for p in routes_dir.iterdir()) == ["other-app@aws", "sample-app@onprem"]
    current = client.get("/api/v1/ingest/traffic/sample-app", headers=AUTH).json()
    assert current["target"] == "onprem" and current["changed_at"]


def test_switch_is_idempotent(client, routes_dir):
    for _ in range(2):
        assert (
            client.post(
                "/api/v1/ingest/traffic/sample-app", json={"target": "aws"}, headers=AUTH
            ).json()["target"]
            == "aws"
        )
    assert [p.name for p in routes_dir.iterdir()] == ["sample-app@aws"]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_requires_the_ingest_token(client, routes_dir, headers):
    assert client.get("/api/v1/ingest/traffic/sample-app", headers=headers).status_code == 401
    response = client.post(
        "/api/v1/ingest/traffic/sample-app", json={"target": "aws"}, headers=headers
    )
    assert response.status_code == 401
    assert list(routes_dir.iterdir()) == []


@pytest.mark.parametrize("target", ["AWS", "../etc", "a b", "", "x" * 64])
def test_rejects_unsafe_target_names(client, routes_dir, target):
    response = client.post(
        "/api/v1/ingest/traffic/sample-app", json={"target": target}, headers=AUTH
    )
    assert response.status_code in (400, 422)
    assert list(routes_dir.iterdir()) == []


@pytest.mark.parametrize("app", ["Sample", "a..b", "x" * 64])
def test_rejects_unsafe_app_names(client, routes_dir, app):
    response = client.post(f"/api/v1/ingest/traffic/{app}", json={"target": "aws"}, headers=AUTH)
    assert response.status_code in (400, 422)
    assert list(routes_dir.iterdir()) == []


def test_not_configured_without_a_routes_dir(monkeypatch):
    monkeypatch.setenv("ONEPORT_INGEST_TOKEN", TOKEN)
    monkeypatch.delenv("ONEPORT_TRAFFIC_DIR", raising=False)
    client = TestClient(create_app(auth_mode="demo"))
    assert client.get("/api/v1/ingest/traffic/sample-app", headers=AUTH).status_code == 503


def test_latest_marker_wins_if_two_are_left_behind(tmp_path):
    routes = TrafficRoutes(tmp_path)
    (tmp_path / "sample-app@aws").write_text("")
    os.utime(tmp_path / "sample-app@aws", (1, 1))
    (tmp_path / "sample-app@onprem").write_text("")
    assert routes.current("sample-app").target == "onprem"
