from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient

from controlplane.core.config import AuthMode
from controlplane.main import create_app
from controlplane.schemas.accounts import Identity
from controlplane.services.registered_environments import (
    registered_environment,
    registered_environments,
)

ORIGIN = {"Origin": "http://127.0.0.1:4320"}
ENV_ID = "connected-" + "a" * 22


def facts(version="v1.31.4+k3s1", nodes="2"):
    return [
        {"label": "Kubernetes 버전", "value": version},
        {"label": "노드 수", "value": nodes},
        {"label": "네임스페이스 수", "value": "12"},
        {"label": "스토리지 클래스 수", "value": "1"},
    ]


def snapshot(**overrides):
    base = {
        "id": "a" * 22,
        "environmentId": ENV_ID,
        "target": {
            "organization": "team-a",
            "project": "billing",
            "name": "홈랩 k3s",
            "kind": "kubernetes",
            "region": "",
            "role": "source",
        },
        "device": "APPROVED",
        "tunnel": "READY",
        "collectedAt": 1_794_000_000_000,
        "collected": [{"id": "k8s-x", "kind": "kubernetes", "facts": facts()}],
        "workloads": {
            "coverage": "complete",
            "items": [
                {
                    "namespace": "shop",
                    "kind": "Deployment",
                    "name": "web_api",
                    "replicas": 3,
                    "images": [],
                    "secrets": [],
                    "configMaps": [],
                    "pvcs": [],
                }
            ],
        },
    }
    return {**base, **overrides}


def test_a_registered_cluster_becomes_an_environment_with_its_inventory():
    env = registered_environment(snapshot())
    assert (env.id, env.name, env.project) == (ENV_ID, "홈랩 k3s", "billing")
    assert (env.provider, env.kind, env.role, env.region) == ("onprem", "k3s", "legacy", "-")
    assert env.registered and env.connected and env.issues == []
    assert (env.cluster.version, env.cluster.node_count) == ("v1.31.4+k3s1", 2)
    assert env.observed_at.year == 2026
    [app] = env.components
    assert (app.id, app.name, app.namespace, app.layer) == (
        "shop-deployment-web-api",
        "web_api",
        "shop",
        "app",
    )
    assert "복제본 3개" in app.detail


def test_cloud_and_plain_kubernetes_kinds_and_unknown_node_counts():
    gke = registered_environment(
        snapshot(
            target={**snapshot()["target"], "kind": "gcp", "region": "asia-northeast3"},
            collected=[{"facts": facts(version="v1.30.5-gke.1", nodes="미확인")}],
        )
    )
    assert (gke.provider, gke.kind, gke.region, gke.role) == (
        "gcp",
        "gke",
        "asia-northeast3",
        "legacy",
    )
    assert gke.cluster.node_count is None
    plain = registered_environment(snapshot(collected=[{"facts": facts(version="v1.29.0")}]))
    assert plain.kind == "kubernetes"
    target_role = registered_environment(
        snapshot(target={**snapshot()["target"], "role": "target"})
    )
    assert target_role.role == "target"


def test_disconnected_registrations_stay_visible_with_a_warning():
    env = registered_environment(snapshot(tunnel="CLOSED", workloads=None))
    assert env.connected is False
    assert [issue.title for issue in env.issues] == ["보안 연결이 끊겼어요"]
    assert env.components == []


def test_unfinished_or_unknown_registrations_are_left_out():
    assert registered_environment(snapshot(environmentId=None)) is None
    assert registered_environment(snapshot(target=None)) is None
    assert registered_environment(snapshot(collected=[])) is None
    assert registered_environment(snapshot(target={"kind": "azure"})) is None


def connections(rows, seen):
    def listing(actor):
        seen.append((actor.team, actor.can_write))
        return rows

    return SimpleNamespace(runtime_tenant_id="demo-team", list=listing)


def test_registered_environments_use_the_viewer_team_or_the_demo_tenant():
    seen = []
    viewer = Identity(email="a@example.com", team="team-a")
    assert (
        len(registered_environments(connections([snapshot()], seen), AuthMode.LOGIN, viewer)) == 1
    )
    assert len(registered_environments(connections([snapshot()], seen), AuthMode.DEMO, None)) == 1
    assert registered_environments(connections([snapshot()], seen), AuthMode.LOGIN, None) == []
    assert registered_environments(None, AuthMode.DEMO, None) == []
    assert seen == [("team-a", False), ("demo-team", False)]


def test_a_failing_connection_service_does_not_hide_the_fleet():
    def broken(actor):
        raise RuntimeError("store down")

    service = SimpleNamespace(runtime_tenant_id="demo-team", list=broken)
    assert registered_environments(service, AuthMode.DEMO, None) == []


def test_fleet_endpoint_lists_registered_environments_after_the_fixed_ones(monkeypatch):
    monkeypatch.delenv("ONEPORT_ACCESS_PIN", raising=False)
    app = create_app(auth_mode="demo")
    client = TestClient(app, headers=ORIGIN)
    before = [env["id"] for env in client.get("/api/v1/fleet").json()["environments"]]
    duplicate = snapshot(environmentId=before[0])
    app.state.container = replace(
        app.state.container, connections=connections([snapshot(), duplicate], [])
    )
    environments = client.get("/api/v1/fleet").json()["environments"]
    assert [env["id"] for env in environments] == [*before, ENV_ID]
    added = environments[-1]
    assert (added["registered"], added["project"], added["kind"]) == (True, "billing", "k3s")
    assert all(env["registered"] is False for env in environments[:-1])
