import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from kubernetes.client.exceptions import ApiException

from controlplane.domain.cluster import cluster_view
from controlplane.domain.fleet import ClusterFacts, EnvironmentSnapshot, Resources
from controlplane.integrations import kube
from controlplane.integrations.codec import facts_from_json, facts_to_json
from controlplane.integrations.defaults import GCP, ONPREM
from controlplane.integrations.kube_nodes import node_facts, pod_requests, quantity
from controlplane.services.fleet import environment_view

NOW = datetime(2026, 11, 6, tzinfo=UTC)
GIB = 2**30


def node(name, *, ready=True, cpu="2", memory="4Gi", role=None, zone="zone-a"):
    labels = {"topology.kubernetes.io/zone": zone, "node.kubernetes.io/instance-type": "m.large"}
    if role:
        labels[f"node-role.kubernetes.io/{role}"] = "true"
    return {
        "metadata": {"name": name, "labels": labels},
        "spec": {},
        "status": {
            "allocatable": {"cpu": cpu, "memory": memory},
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "nodeInfo": {"kubeletVersion": "v1.35.4", "osImage": "Ubuntu", "architecture": "amd64"},
        },
    }


def running(node_name, *, cpu="250m", memory="256Mi", phase="Running", init=None):
    spec = {
        "nodeName": node_name,
        "containers": [
            {"resources": {"requests": {"cpu": cpu, "memory": memory}}},
            {"resources": {}},
        ],
    }
    if init:
        spec["initContainers"] = [{"resources": {"requests": init}}]
    return {"spec": spec, "status": {"phase": phase}}


def used(name, cpu, memory):
    return {"metadata": {"name": name}, "usage": {"cpu": cpu, "memory": memory}}


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        ("250m", 0.25),
        ("2", 2),
        ("1.5", 1.5),
        ("903407760n", 0.90340776),
        ("3413504Ki", 3413504 * 1024),
        ("1Gi", GIB),
        ("500M", 500_000_000),
        ("1e3", 1000),
        ("lots", 0),
        ("5Q", 0),
        (None, 0),
    ],
)
def test_kubernetes_quantities_are_read_in_base_units(raw, value):
    assert float(quantity(raw)) == pytest.approx(value)


def test_pod_requests_count_the_larger_of_containers_and_init_containers():
    assert pod_requests(running("n1")) == Resources(250, 256 * 2**20)
    heavy_init = running("n1", init={"cpu": "1", "memory": "64Mi"})
    assert pod_requests(heavy_init) == Resources(1000, 256 * 2**20)


def test_nodes_combine_the_node_list_pod_requests_and_live_usage():
    pods = [running("n1"), running("n1"), running("n1", phase="Succeeded"), running("")]
    [n1, n2] = node_facts(
        [node("n1", role="control-plane"), node("n2", ready=False)],
        pods,
        [used("n1", "500m", "1Gi")],
    )
    assert (n1.ready, n1.roles, n1.zone, n1.instance_type) == (
        True,
        ["control-plane"],
        "zone-a",
        "m.large",
    )
    assert n1.allocatable == Resources(2000, 4 * GIB)
    assert (n1.requests, n1.pods) == (Resources(500, 512 * 2**20), 2)
    assert n1.usage == Resources(500, GIB)
    assert (n2.ready, n2.pods, n2.usage) == (False, 0, None)


def test_without_the_node_list_nodes_still_come_from_pods_and_usage():
    nodes = node_facts(None, [running("k3s-worker")], [used("k3s-master", "1", "2Gi")])
    assert [n.name for n in nodes] == ["k3s-master", "k3s-worker"]
    assert nodes[0].allocatable is None and nodes[0].ready is None
    view = cluster_view(ClusterFacts(observed_at=NOW, connected=True, nodes=nodes))
    assert (view.node_count, view.ready_nodes, view.allocatable) == (2, None, None)
    assert view.usage is None
    assert view.requests.cpu_millicores == 250


def test_nothing_readable_is_unknown_not_an_empty_cluster():
    assert node_facts(None, None, None) is None
    assert node_facts([], [], []) == []
    [listed] = node_facts([node("n1")], None, None)
    assert (listed.requests, listed.pods, listed.usage) == (None, None, None)
    view = cluster_view(ClusterFacts(observed_at=NOW, connected=True, version="v1.35.4"))
    assert (view.version, view.node_count, view.nodes) == ("v1.35.4", None, [])
    assert cluster_view(ClusterFacts(observed_at=NOW, connected=False)) is None


def test_cluster_totals_add_up_every_node():
    nodes = node_facts(
        [node("n1"), node("n2")],
        [running("n1"), running("n2")],
        [used("n1", "500m", "1Gi"), used("n2", "250m", "1Gi")],
    )
    facts = ClusterFacts(observed_at=NOW, connected=True, version="v1.35.4", nodes=nodes)
    view = environment_view(EnvironmentSnapshot(GCP, facts)).cluster
    assert (view.node_count, view.ready_nodes) == (2, 2)
    assert view.allocatable.model_dump() == {"cpu_millicores": 4000, "memory_bytes": 8 * GIB}
    assert view.usage.model_dump() == {"cpu_millicores": 750, "memory_bytes": 2 * GIB}


def test_nodes_survive_the_journal():
    nodes = node_facts([node("n1")], [running("n1")], [used("n1", "500m", "1Gi")])
    facts = ClusterFacts(observed_at=NOW, connected=True, version="v1.35.4", nodes=nodes)
    payload = facts_to_json(facts)
    json.dumps(payload)
    restored = facts_from_json(payload)
    assert (restored.version, restored.nodes) == ("v1.35.4", nodes)


def test_reader_falls_back_to_pods_when_nodes_and_metrics_are_closed(monkeypatch):
    monkeypatch.setattr(kube, "api_client", lambda env: kube.client.ApiClient())
    reader = kube.ClusterReader(ONPREM)

    def forbidden(*args):
        raise ApiException(status=403)

    reader._core = SimpleNamespace(list_node=forbidden)
    reader._custom = SimpleNamespace(list_cluster_custom_object=forbidden)
    [worker] = reader._nodes([running("k3s-worker")])
    assert (worker.name, worker.pods, worker.usage, worker.allocatable) == (
        "k3s-worker",
        1,
        None,
        None,
    )
