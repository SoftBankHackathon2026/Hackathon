from datetime import UTC, datetime

from controlplane.domain.fleet import ClusterFacts, EnvironmentSnapshot
from controlplane.domain.topology import environment_components
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

NOW = datetime(2026, 11, 6, tzinfo=UTC)


def workload(kind, ns, name, *, ready=1, labels=None, env=None, image="app:1", configmaps=()):
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
                "replicas": 1,
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
