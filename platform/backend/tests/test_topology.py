from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from kubernetes.client.exceptions import ApiException
from kubernetes.config import ConfigException
from urllib3.exceptions import MaxRetryError

from controlplane.domain.fleet import ClusterFacts, EnvironmentSnapshot
from controlplane.domain.topology import environment_components
from controlplane.integrations import kube
from controlplane.integrations.codec import facts_from_json, facts_to_json
from controlplane.integrations.config import with_default_layout
from controlplane.integrations.defaults import AWS, ONPREM
from controlplane.integrations.kube_inventory import (
    ClusterObjects,
    IngressSpec,
    ServiceSpec,
    WorkloadSpec,
    build_inventory,
)
from controlplane.services import security
from controlplane.services.fleet import environment_view

NOW = datetime(2026, 11, 6, tzinfo=UTC)


def workload(
    kind, ns, name, *, ready=1, replicas=1, labels=None, env=None, image="app:1", configmaps=()
):
    container = {
        "image": image,
        "env": [{"name": k, "value": v} for k, v in (env or {}).items()],
        "envFrom": [{"configMapRef": {"name": cm}} for cm in configmaps],
    }
    return WorkloadSpec.from_k8s(
        kind,
        {
            "metadata": {"namespace": ns, "name": name, "labels": labels or {}},
            "spec": {
                "replicas": replicas,
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {"containers": [container]},
                },
            },
            "status": {"readyReplicas": ready},
        },
    )


def service(ns, name, app):
    return ServiceSpec(namespace=ns, name=name, selector={"app": app})


def traefik_ingress(ns, backend):
    path = {"backend": {"service": {"name": backend}}}
    return IngressSpec.from_k8s(
        {
            "metadata": {"namespace": ns},
            "spec": {"ingressClassName": "traefik", "rules": [{"http": {"paths": [path]}}]},
        }
    )


def live_components(env, inventory):
    env = with_default_layout(env.model_copy(update={"components": []}))
    facts = ClusterFacts(observed_at=NOW, connected=True, inventory=inventory)
    return {c.id: c for c in environment_components(EnvironmentSnapshot(env, facts))}


SHOP = ClusterObjects(
    workloads=[
        workload("deployment", "argocd", "argocd-server"),
        workload("statefulset", "argocd", "argocd-application-controller", ready=0),
        workload("deployment", "kube-system", "traefik"),
        workload("deployment", "kube-system", "coredns"),
        workload(
            "deployment",
            "shop",
            "api",
            env={"DB": "postgres://u@db.shop.svc:5432/x"},
            configmaps=["api-cfg"],
        ),
        workload("statefulset", "shop", "kafka-broker", image="apache/kafka:3.9"),
        workload("statefulset", "shop", "kafka-controller", image="apache/kafka:3.9"),
        workload("statefulset", "shop", "db", image="postgres:16"),
        workload(
            "deployment",
            "shop",
            "rds-user",
            env={"HOST": "oneaction-review-db.x.rds.amazonaws.com"},
        ),
    ],
    services=[
        service("shop", "db", "db"),
        service("shop", "kafka", "kafka-broker"),
        service("shop", "api", "api"),
    ],
    ingresses=[traefik_ingress("shop", "api")],
    configmaps={("shop", "api-cfg"): ["KAFKA=kafka:9092"]},
)


def test_components_come_from_the_cluster_with_calls_and_entry_paths():
    found = live_components(ONPREM, build_inventory(SHOP, {"rds": ["oneaction-review-db."]}))
    assert "coredns" not in found
    assert found["argocd"].status == "degraded"
    assert "워크로드 2개" in found["argocd"].detail
    assert found["traefik"].layer == "entry"
    assert (found["kafka"].kind, found["db"].kind) == ("kafka", "postgres")
    assert found["api"].depends_on == ["db", "kafka", "traefik"]
    assert found["api"].placement == ["k3s 클러스터", "ns shop"]
    assert "tailscale-onprem" in found
    assert found["rds-user"].depends_on == []


def test_external_calls_link_only_to_components_the_environment_declares():
    inventory = build_inventory(SHOP, {c.id: c.endpoint_hints for c in AWS.components})
    found = live_components(AWS, inventory)
    assert found["rds-user"].depends_on == ["rds"]
    assert found["api"].placement[-2:] == ["EKS oneaction", "ns shop"]


def test_inventory_survives_the_journal_and_keeps_no_config_values():
    objects = ClusterObjects(
        workloads=[
            workload("deployment", "shop", "api", env={"DB_URL": "postgres://user:pw@db:5432"})
        ],
        services=[service("shop", "db", "db")],
        ingresses=[],
        configmaps={},
    )
    inventory = build_inventory(objects, {})
    payload = facts_to_json(ClusterFacts(observed_at=NOW, connected=True, inventory=inventory))
    assert "pw@" not in str(payload)
    assert facts_from_json(payload).inventory == inventory


def test_without_inventory_the_configured_components_are_drawn():
    facts = ClusterFacts(observed_at=NOW, connected=True)
    views = environment_components(EnvironmentSnapshot(ONPREM, facts))
    assert [v.id for v in views] == [c.id for c in ONPREM.components]


def test_connected_cluster_that_lists_nothing_explains_the_missing_read_access():
    env = with_default_layout(AWS.model_copy(update={"components": []}))
    view = environment_view(EnvironmentSnapshot(env, ClusterFacts(observed_at=NOW, connected=True)))
    assert [i.title for i in view.issues] == ["클러스터 구성 상태를 읽지 못했어요"]


def test_reader_reports_disconnected_when_the_cluster_drops_mid_read(monkeypatch):
    monkeypatch.setattr(kube, "api_client", lambda env: kube.client.ApiClient())
    reader = kube.ClusterReader(ONPREM)
    reader._core = SimpleNamespace(
        read_namespace=lambda ns: SimpleNamespace(metadata=SimpleNamespace(labels={}))
    )

    def dropped(namespace):
        raise MaxRetryError(None, "/apis/networking.k8s.io")

    reader._net = SimpleNamespace(list_namespaced_network_policy=dropped)
    assert reader.read().connected is False


def test_reader_without_kubeconfig_does_not_fall_back_to_the_default_one(monkeypatch):
    monkeypatch.setattr(kube.config, "new_client_from_config", lambda **kw: kube.client.ApiClient())
    with pytest.raises(ConfigException):
        kube.api_client(ONPREM.model_copy(update={"kubeconfig": None, "in_cluster": False}))


def test_permission_errors_are_not_reported_as_an_unreachable_cluster(monkeypatch):
    monkeypatch.setattr(kube, "api_client", lambda env: kube.client.ApiClient())
    reader = kube.ClusterReader(AWS)

    def forbidden(namespace):
        raise ApiException(status=403)

    reader._core = SimpleNamespace(read_namespace=forbidden)
    facts = reader.read()
    view = environment_view(EnvironmentSnapshot(AWS, facts))
    assert (facts.connected, facts.read_status) == (False, 403)
    assert [i.title for i in view.issues] == ["클러스터를 읽을 권한이 없어요"]
    assert facts_from_json(facts_to_json(facts)).read_status == 403


def test_workload_scaled_to_zero_is_idle_not_an_outage():
    objects = ClusterObjects(
        workloads=[workload("deployment", "shop", "batch", ready=0, replicas=0)],
        services=[],
        ingresses=[],
        configmaps={},
    )
    env = with_default_layout(ONPREM.model_copy(update={"components": []}))
    facts = ClusterFacts(observed_at=NOW, connected=True, inventory=build_inventory(objects, {}))
    view = environment_view(EnvironmentSnapshot(env, facts))
    batch = next(c for c in view.components if c.id == "batch")
    assert batch.status == "idle"
    assert view.issues == []


def test_ingress_links_survive_a_product_running_in_two_namespaces():
    objects = ClusterObjects(
        workloads=[
            workload("deployment", "kube-system", "traefik"),
            workload("deployment", "shop", "ingress-nginx-controller"),
            workload("deployment", "edge", "ingress-nginx-controller"),
            workload("deployment", "shop", "api"),
        ],
        services=[service("shop", "api", "api")],
        ingresses=[traefik_ingress("shop", "api")],
        configmaps={},
    )
    found = live_components(ONPREM, build_inventory(objects, {}))
    assert {"ingress-nginx-shop", "ingress-nginx-edge"} <= found.keys()
    assert found["api"].depends_on == ["traefik"]


def test_security_posture_builds_each_environment_topology_once(monkeypatch):
    calls = []

    def counted(snapshot):
        calls.append(snapshot.config.id)
        return environment_components(snapshot)

    monkeypatch.setattr(security, "environment_components", counted)
    inventory = build_inventory(SHOP, {})
    snapshots = [
        EnvironmentSnapshot(
            with_default_layout(env.model_copy(update={"components": []})),
            ClusterFacts(observed_at=NOW, connected=True, inventory=inventory),
        )
        for env in (ONPREM, AWS)
    ]
    fleet = SimpleNamespace(mode="live", list_environments=lambda: snapshots)
    posture = security.SecurityService(fleet).get_posture()
    assert calls == ["onprem", "aws"]
    wl01 = next(c for c in posture.controls if c.id == "WL-01")
    assert "api" in wl01.results["onprem"].resources
