from types import SimpleNamespace as N

import pytest
from kubernetes import client, config

from migration.harbor_infra.cluster import collect
from migration.harbor_infra.config import ConfigError


def fixture_cluster(monkeypatch):
    node = N(
        metadata=N(name="worker", uid="uid", labels={}),
        status=N(
            conditions=[N(type="Ready", status="True")],
            addresses=[N(address="192.0.2.1")],
            node_info=N(machine_id="machine"),
        ),
    )
    deployment = N(
        metadata=N(
            name="harbor-core",
            uid="deployment",
            namespace="registry",
            labels={"app.kubernetes.io/instance": "harbor"},
            owner_references=[],
        ),
        spec=N(
            template=N(spec=N(containers=[N(image="goharbor/harbor-core:v2.14.6")]))
        ),
    )

    class Reader:
        def __getattr__(self, name):
            if name == "list_node":
                return lambda **kw: N(items=[node])
            if "deployment" in name:
                return lambda *a, **kw: N(items=[deployment])
            if "secret" in name:
                pytest.fail("must never read Secrets")
            return lambda *a, **kw: N(items=[])

    reader = Reader()
    monkeypatch.setattr(config, "load_kube_config", lambda *a, **kw: None)
    monkeypatch.setattr(client, "CoreV1Api", lambda *a: reader)
    monkeypatch.setattr(client, "AppsV1Api", lambda *a: reader)
    monkeypatch.setattr(client, "NetworkingV1Api", lambda *a: reader)
    c = {
        "cluster": {
            "kubeconfig_ref": "unused",
            "context": "selected",
            "namespaces": [],
        },
        "nodes": [{"name": "worker", "uid": "uid", "ssh_host": "192.0.2.1"}],
    }
    return c, node, reader


def test_zero_replica_installation_discovery_without_secret_reads(monkeypatch):
    c, _, _ = fixture_cluster(monkeypatch)
    report = collect(c, True)
    assert report["candidates"][0]["name"] == "harbor"
    assert report["candidates"][0]["components"][0]["kind"] == "deployment"
    assert report["candidates"][0]["product_confirmed"] is False
    assert len(report["coverage"]) == 7


@pytest.mark.parametrize("change", ["uid", "address", "ready", "missing"])
def test_node_identity_mismatch_is_blocked(monkeypatch, change):
    c, node, _ = fixture_cluster(monkeypatch)
    if change == "uid":
        node.metadata.uid = "replacement"
    if change == "address":
        node.status.addresses[0].address = "192.0.2.2"
    if change == "ready":
        node.status.conditions[0].status = "False"
    if change == "missing":
        c["nodes"][0]["name"] = "other"
    with pytest.raises(ConfigError):
        collect(c)


def test_partial_kubernetes_reads_are_not_absence(monkeypatch):
    c, _, reader = fixture_cluster(monkeypatch)

    def deny(**kwargs):
        raise PermissionError("PRIVATE PROVIDER BODY")

    reader.list_service_for_all_namespaces = deny
    report = collect(c, True)
    assert "KUBERNETES_DISCOVERY_INCOMPLETE" in report["reason_codes"]
    assert any(
        v["resource"] == "service" and v["status"] == "unavailable"
        for v in report["coverage"]
    )
    assert "PRIVATE PROVIDER BODY" not in str(report)
