from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from kubernetes.config import ConfigException

from controlplane.domain.fleet import (
    ClusterFacts,
    EnvironmentConfig,
    EnvironmentSnapshot,
    FleetConfig,
)
from controlplane.integrations import kube
from controlplane.integrations.defaults import AWS, DEFAULT_FLEET, GCP, ONPREM
from controlplane.main import create_app
from controlplane.providers.loader import load_platform
from controlplane.services.security import SecurityService


def test_fleet_config_accepts_gke_environment_and_in_cluster_reader():
    config = FleetConfig.model_validate(
        {
            "mode": "live",
            "environments": [
                {
                    "id": "gcp",
                    "name": "GCP GKE",
                    "kind": "gke",
                    "provider": "gcp",
                    "region": "asia-northeast3",
                    "role": "target",
                    "kubeconfig": "/etc/oneport/kube/gcp.kubeconfig",
                    "context": "oneaction-gke",
                    "app_namespace": "platform",
                },
                {
                    "id": "aws",
                    "name": "AWS EKS",
                    "kind": "eks",
                    "provider": "aws",
                    "region": "ap-northeast-2",
                    "role": "target",
                    "in_cluster": True,
                    "app_namespace": "platform",
                },
            ],
            "migrations": [],
        }
    )
    assert config.environment("gcp").kind == "gke"
    assert config.environment("aws").in_cluster is True
    assert config.environment("gcp").in_cluster is False


def test_in_cluster_reader_uses_its_own_configuration(monkeypatch):
    seen = {}

    def fake_incluster(client_configuration):
        client_configuration.host = "https://10.100.0.1"
        seen["configuration"] = client_configuration

    monkeypatch.setattr(kube.config, "load_incluster_config", fake_incluster)
    env = AWS.model_copy(update={"in_cluster": True})
    api = kube.api_client(env)
    assert api.configuration is seen["configuration"]
    assert api.configuration.host == "https://10.100.0.1"


def test_in_cluster_reader_outside_a_cluster_raises_config_error():
    # 수집기는 ConfigException 을 잡아 해당 환경만 경고로 남긴다.
    with pytest.raises(ConfigException):
        kube.api_client(GCP.model_copy(update={"in_cluster": True}))


def test_default_fleet_draws_gcp_as_a_second_target():
    assert [e.id for e in DEFAULT_FLEET.environments] == ["onprem", "aws", "gcp"]
    assert GCP.boundary[0].startswith("GCP 프로젝트")
    names = {c.id: c for c in GCP.components}
    assert names["cloud-sql"].placement == ["VPC oneaction-dev-vpc", "data 서브넷"]
    assert names["review-service"].placement[-1] == "ns platform"


def test_security_posture_compares_source_with_every_target():
    weak_gcp = GCP.model_copy(
        update={"declared": GCP.declared.model_copy(update={"db_backups": False})}
    )
    snapshots = [
        EnvironmentSnapshot(env, ClusterFacts(observed_at=None, connected=False))
        for env in (ONPREM, AWS, weak_gcp)
    ]
    fleet = SimpleNamespace(mode="simulated", list_environments=lambda: snapshots)
    posture = SecurityService(fleet).get_posture()
    pairs = [(p.source_env, p.target_env) for p in posture.parities]
    assert pairs == [("onprem", "aws"), ("onprem", "gcp")]
    assert posture.parity.target_env == "aws"
    gcp = next(p for p in posture.parities if p.target_env == "gcp")
    assert "DATA-03" in gcp.regressions
    assert "CIS-GCP" in {s.id for s in posture.standards}


def test_simulated_api_serves_three_environments(monkeypatch):
    for name in ("ONEPORT_CONFIG", "ONEPORT_RUNTIME_CONFIG", "ONEPORT_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app()) as client:
        fleet = client.get("/api/v1/fleet").json()
        posture = client.get("/api/v1/security/posture").json()
    assert {e["id"]: e["kind"] for e in fleet["environments"]} == {
        "onprem": "k3s",
        "aws": "eks",
        "gcp": "gke",
    }
    assert [p["target_env"] for p in posture["parities"]] == ["aws", "gcp"]


def test_dashboard_database_without_deployment_providers(monkeypatch, tmp_path):
    for name in ("ONEPORT_CONFIG", "ONEPORT_RUNTIME_CONFIG", "ONEPORT_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert load_platform() is None
    secret = tmp_path / "database-url"
    secret.write_text("postgresql://user:pass@db.internal:5432/oneport")
    secret.chmod(0o600)
    monkeypatch.setenv("ONEPORT_DATABASE_URL_FILE", str(secret))
    loaded = load_platform()
    assert loaded is not None
    assert loaded.store.dsn == "postgresql://user:pass@db.internal:5432/oneport"
    assert loaded.registry.runtimes() == set()


def test_live_dashboard_with_dashboard_only_database(store, monkeypatch, tmp_path):
    # 배포 Provider 없이도 operator 모드에서 토큰 없이 뜨고, 실측 저널을 읽는다.
    for name in ("ONEPORT_CONFIG", "ONEPORT_RUNTIME_CONFIG", "ONEPORT_OPERATOR_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ONEPORT_DATABASE_URL", store.dsn)
    fleet = DEFAULT_FLEET.model_copy(update={"mode": "live"})
    path = tmp_path / "fleet.json"
    path.write_text(fleet.model_dump_json())
    monkeypatch.setenv("ONEPORT_FLEET_CONFIG", str(path))
    with TestClient(create_app(auth_mode="operator")) as client:
        body = client.get("/api/v1/fleet").json()
    assert body["source"] == "live"
    assert [e["id"] for e in body["environments"]] == ["onprem", "aws", "gcp"]


def test_environment_config_rejects_unknown_provider():
    with pytest.raises(ValueError):
        EnvironmentConfig.model_validate(
            {
                "id": "azure",
                "name": "AKS",
                "kind": "aks",
                "provider": "azure",
                "region": "koreacentral",
                "role": "target",
                "app_namespace": "platform",
            }
        )


def test_status_clients_fail_over_to_the_node_that_answers():
    import httpx

    from controlplane.integrations import http_clients

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "172.30.1.10":
            raise httpx.ConnectTimeout("no local endpoint", request=request)
        if request.url.path == "/connectors/forward_001-source/status":
            return httpx.Response(200, json={"connector": {"state": "RUNNING"}, "tasks": []})
        return httpx.Response(404)

    http_clients._PREFERRED.clear()
    urls = "http://172.30.1.10:30883, http://172.30.1.7:30883/"
    client = httpx.Client(transport=httpx.MockTransport(handler))
    found = http_clients.ConnectStatusClient(urls, client).get_status("forward_001-source")
    assert found == {"connector": {"state": "RUNNING"}, "tasks": []}
    assert calls == ["172.30.1.10", "172.30.1.7"]
    # 다음 tick 은 응답한 노드부터 시도한다.
    calls.clear()
    http_clients.ConnectStatusClient(urls, client).get_status("forward_001-source")
    assert calls == ["172.30.1.7"]
    # HTTP 응답(404)은 다음 주소로 넘기지 않는다.
    calls.clear()
    assert http_clients.ConnectStatusClient(urls, client).get_status("missing") is None
    assert calls == ["172.30.1.7"]


def test_status_client_raises_when_no_address_answers():
    import httpx

    from controlplane.integrations import http_clients

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    http_clients._PREFERRED.clear()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.ConnectError):
        http_clients.WorkerStatusClient("http://a:1,http://b:2", client).get_status()


def test_live_config_without_components_inherits_default_layout(monkeypatch, tmp_path):
    from controlplane.integrations.config import load_fleet_config

    raw = {
        "mode": "live",
        "environments": [
            {
                "id": "gcp",
                "name": "GCP",
                "kind": "gke",
                "provider": "gcp",
                "region": "asia-northeast3",
                "role": "target",
                "app_namespace": "platform",
            },
            {
                "id": "custom",
                "name": "직접 정의",
                "kind": "k3s",
                "provider": "onprem",
                "region": "lab",
                "role": "legacy",
                "app_namespace": "apps",
            },
        ],
        "migrations": [],
    }
    import json

    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setenv("ONEPORT_FLEET_CONFIG", str(path))
    config = load_fleet_config()
    assert [c.id for c in config.environment("gcp").components] == [c.id for c in GCP.components]
    assert config.environment("gcp").boundary == GCP.boundary
    assert config.environment("custom").components == []
