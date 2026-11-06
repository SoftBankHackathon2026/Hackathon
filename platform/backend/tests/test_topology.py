import json
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
    ConfigValue,
    IngressSpec,
    ServiceSpec,
    WorkloadSpec,
    build_inventory,
    pod_fact,
    pods_by_workload,
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
    configmaps={("shop", "api-cfg"): [ConfigValue("KAFKA", "kafka:9092")]},
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


def test_reader_fetches_only_the_configmaps_workloads_reference(monkeypatch):
    monkeypatch.setattr(kube, "api_client", lambda env: kube.client.ApiClient())
    reader = kube.ClusterReader(ONPREM)
    fetched = []

    def read_namespaced_config_map(name, namespace):
        fetched.append((namespace, name))
        return SimpleNamespace(data={"KAFKA": "kafka:9092"})

    def list_everything():
        raise AssertionError("must not list every ConfigMap")

    reader._core = SimpleNamespace(
        read_namespaced_config_map=read_namespaced_config_map,
        list_config_map_for_all_namespaces=list_everything,
    )
    values = reader._configmap_values(SHOP.workloads)
    assert fetched == [("shop", "api-cfg")]
    assert values == {("shop", "api-cfg"): [ConfigValue("KAFKA", "kafka:9092")]}


def test_only_host_shaped_references_become_calls():
    objects = ClusterObjects(
        workloads=[
            workload("deployment", "shop", "user-only", env={"DB_USERNAME": "postgres"}),
            workload("deployment", "shop", "host-key", env={"DB_HOST": "postgres"}),
            workload("deployment", "shop", "url", env={"DSN": "postgresql://app@postgres/db"}),
            workload("deployment", "shop", "registry", configmaps=["oneport-fleet"]),
            workload("deployment", "shop", "yaml", configmaps=["worker-cfg"]),
            workload("deployment", "shop", "repo", env={"REPOS": "acme/postgres"}),
            workload("statefulset", "shop", "postgres", image="postgres:16"),
        ],
        services=[service("shop", "postgres", "postgres")],
        ingresses=[],
        configmaps={
            ("shop", "oneport-fleet"): [ConfigValue("fleet.json", "postgres:5432 s3")],
            ("shop", "worker-cfg"): [
                ConfigValue("config.yaml", "db:\n  host: postgres  # ns\n  admin_dbname: other\n")
            ],
        },
    )
    found = live_components(AWS, build_inventory(objects, {"s3": ["s3"]}))
    assert found["user-only"].depends_on == []
    assert found["host-key"].depends_on == ["postgres"]
    assert found["url"].depends_on == ["postgres"]
    assert found["registry"].depends_on == []
    assert found["yaml"].depends_on == ["postgres"]
    assert found["repo"].depends_on == []


def pod(ns, name, owner, *, phase="Running", ready=True, waiting=None, restarts=0):
    state = {"waiting": {"reason": waiting}} if waiting else {"running": {}}
    return {
        "metadata": {
            "namespace": ns,
            "name": name,
            "ownerReferences": [{"kind": owner[0], "name": owner[1], "controller": True}],
        },
        "spec": {
            "nodeName": "worker-1",
            "containers": [
                {"name": "app", "image": "app:1"},
                {"name": "proxy", "image": "envoy:1"},
            ],
        },
        "status": {
            "phase": phase,
            "startTime": "2026-11-06T01:02:03Z",
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "containerStatuses": [
                {"name": "proxy", "ready": True, "restartCount": 1, "state": {"running": {}}},
                {"name": "app", "ready": ready, "restartCount": restarts, "state": state},
            ],
        },
    }


SHOP_PODS = [
    pod("shop", "api-7d9f-abcde", ("ReplicaSet", "api-7d9f")),
    pod(
        "shop",
        "api-7d9f-fghij",
        ("ReplicaSet", "api-7d9f"),
        ready=False,
        waiting="CrashLoopBackOff",
        restarts=7,
    ),
    pod("shop", "web-canary-5c4b-xyz", ("ReplicaSet", "web-canary-5c4b"), phase="Pending"),
    pod("shop", "db-0", ("StatefulSet", "db")),
    pod("shop", "migrate-x1", ("Job", "migrate")),
    pod("argocd", "argocd-server-6f-q", ("ReplicaSet", "argocd-server-6f")),
    pod(
        "argocd",
        "argocd-application-controller-0",
        ("StatefulSet", "argocd-application-controller"),
    ),
]


def test_pods_are_grouped_under_the_workload_that_owns_them():
    grouped = pods_by_workload(SHOP_PODS)
    assert "shop/migrate" not in grouped
    assert [p.name for p in grouped["shop/web-canary"]] == ["web-canary-5c4b-xyz"]
    crashing = grouped["shop/api"][1]
    assert (crashing.reason, crashing.restarts, crashing.node) == (
        "CrashLoopBackOff",
        8,
        "worker-1",
    )
    assert [(c.name, c.ready, c.image) for c in crashing.containers] == [
        ("app", False, "app:1"),
        ("proxy", True, "envoy:1"),
    ]
    assert grouped["shop/db"][0].started_at == datetime(2026, 11, 6, 1, 2, 3, tzinfo=UTC)


def test_each_component_carries_the_pods_of_all_its_workloads():
    objects = ClusterObjects(
        workloads=[
            workload("deployment", "shop", "api", ready=1, replicas=2),
            workload("deployment", "argocd", "argocd-server"),
            workload("statefulset", "argocd", "argocd-application-controller"),
        ],
        services=[],
        ingresses=[],
        configmaps={},
        pods=pods_by_workload(SHOP_PODS),
    )
    found = live_components(ONPREM, build_inventory(objects, {}))
    api = found["api"].model_dump(mode="json")["pods"]
    assert [(p["name"], p["ready"], p["restarts"]) for p in api] == [
        ("api-7d9f-abcde", True, 1),
        ("api-7d9f-fghij", False, 8),
    ]
    assert api[0]["started_at"] == "2026-11-06T01:02:03Z"
    assert [p.name for p in found["argocd"].pods] == [
        "argocd-application-controller-0",
        "argocd-server-6f-q",
    ]
    assert "pods" not in found["tailscale-onprem"].model_dump(mode="json")


def test_pods_that_could_not_be_listed_are_unknown_not_empty():
    objects = ClusterObjects(
        workloads=[workload("deployment", "shop", "api")], services=[], ingresses=[], configmaps={}
    )
    assert live_components(ONPREM, build_inventory(objects, {}))["api"].pods is None
    listed = ClusterObjects(**{**objects.__dict__, "pods": {}})
    assert live_components(ONPREM, build_inventory(listed, {}))["api"].pods == []


def test_pods_survive_the_journal():
    objects = ClusterObjects(
        workloads=[workload("deployment", "shop", "api")],
        services=[],
        ingresses=[],
        configmaps={},
        pods=pods_by_workload(SHOP_PODS),
    )
    inventory = build_inventory(objects, {})
    payload = facts_to_json(ClusterFacts(observed_at=NOW, connected=True, inventory=inventory))
    json.dumps(payload)
    assert facts_from_json(payload).inventory == inventory


def test_reader_leaves_pods_unknown_when_it_may_not_list_them(monkeypatch):
    monkeypatch.setattr(kube, "api_client", lambda env: kube.client.ApiClient())
    reader = kube.ClusterReader(ONPREM)

    def forbidden():
        raise ApiException(status=403)

    reader._core = SimpleNamespace(list_pod_for_all_namespaces=forbidden)
    assert reader._pods() is None


def test_a_pod_stuck_in_an_init_container_shows_that_reason():
    stuck = pod("shop", "api-1", ("ReplicaSet", "api-7d9f"), phase="Pending", ready=False)
    stuck["status"]["initContainerStatuses"] = [
        {"state": {"waiting": {"reason": "ImagePullBackOff"}}}
    ]
    assert pod_fact(stuck).reason == "ImagePullBackOff"


def test_an_unexpected_pod_phase_is_reported_as_unknown():
    odd = pod("shop", "api-1", ("ReplicaSet", "api-7d9f"), phase="Evicting")
    assert pod_fact(odd).phase == "Unknown"
