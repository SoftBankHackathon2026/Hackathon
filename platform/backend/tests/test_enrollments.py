import json

from fastapi.testclient import TestClient

from controlplane.main import create_app
from controlplane.providers.registry import ProviderRegistry

ORIGIN = {"Origin": "http://127.0.0.1:4320"}


def report(token_kind: str = "kubernetes") -> dict:
    return {
        "kind": token_kind,
        "context": "hackathon",
        "server": "https://192.0.2.10:6443",
        "version_json": json.dumps({"serverVersion": {"gitVersion": "v1.35.4+k3s1"}}),
        "nodes_tsv": "\n".join(
            [
                "k3s-master|2|4005684Ki|2|4005684Ki|v1.35.4+k3s1|Ubuntu 22.04.5 LTS|5.15.0|containerd://2.2.3|amd64|192.0.2.10",
                "k3s-worker|2|8128300Ki|2|8128300Ki|v1.35.4+k3s1|Ubuntu 22.04.5 LTS|5.15.0|containerd://2.2.3|amd64|192.0.2.11",
            ]
        ),
        "control_plane_nodes": "node/k3s-master",
        "storage_tsv": "local-path|rancher.io/local-path|true",
        "ingress_classes": "traefik",
        "namespace_count": 10,
        "deployments": "argocd/argocd-server\nkube-system/traefik",
        "kubeconfig": "apiVersion: v1\nkind: Config\n",
    }


def test_kubernetes_enrollment_registers_once(store):
    app = create_app(registry=ProviderRegistry(), store=store, auth_mode="demo")
    with TestClient(app) as client:
        ticket = client.post("/api/v1/enrollments", json={"kind": "kubernetes"}, headers=ORIGIN)
        assert ticket.status_code == 201
        body = ticket.json()
        assert body["status"] == "pending" and body["script"]["sha256"]
        script = client.get(body["script"]["path"])
        assert script.status_code == 200 and "oneport-reader" in script.text

        path = f"/api/v1/enrollments/{body['id']}/complete"
        assert client.post(path, json=report()).status_code == 401
        auth = {"Authorization": "Bearer " + body["token"]}
        done = client.post(path, json=report(), headers=auth)
        assert done.status_code == 200, done.text
        inventory = done.json()["inventory"]
        assert inventory["distribution"] == "k3s"
        assert (inventory["control_plane_count"], inventory["worker_count"]) == (1, 1)
        assert inventory["cpu_cores"] == 4
        assert inventory["controllers"] == ["Argo CD", "Traefik"]
        assert client.post(path, json=report(), headers=auth).status_code == 409

        listed = client.get("/api/v1/enrollments").json()
        assert [e["status"] for e in listed] == ["registered"]
        assert "token" not in listed[0] and "access" not in listed[0]


def test_aws_enrollment_needs_platform_principal(store, monkeypatch):
    monkeypatch.delenv("ONEPORT_AWS_PRINCIPAL_ARN", raising=False)
    app = create_app(registry=ProviderRegistry(), store=store, auth_mode="demo")
    with TestClient(app) as client:
        assert (
            client.post("/api/v1/enrollments", json={"kind": "aws"}, headers=ORIGIN).status_code
            == 503
        )
