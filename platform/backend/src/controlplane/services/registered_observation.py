import re
from dataclasses import replace
from datetime import datetime

from controlplane.domain.cluster import cluster_view
from controlplane.domain.fleet import ClusterFacts
from controlplane.domain.topology import pod_view
from controlplane.integrations.kube_inventory import pod_fact, replica_counts
from controlplane.integrations.kube_nodes import node_facts
from controlplane.schemas.migrations import ClusterView, ComponentView

OBSERVATION_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Rollout"}


def component_id(namespace: str, kind: str, name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", f"{namespace}-{kind}-{name}".lower()).strip("-")


def observed_cluster(report: dict, observed_at: datetime | None) -> ClusterView:
    nodes = report["nodes"]
    scopes = report["namespaces"]
    if nodes is None:
        return ClusterView(version=report["version"])

    known = {obj["metadata"]["name"] for obj in nodes}
    pods = None
    if scopes and all(scope["pods"] is not None for scope in scopes):
        pods = [
            obj
            for scope in scopes
            for obj in scope["pods"]
            if obj.get("spec", {}).get("nodeName") in known
        ]
    metrics = [
        obj
        for obj in report["metrics"] or []
        if obj["metadata"]["name"] in known and {"cpu", "memory"} <= obj.get("usage", {}).keys()
    ]
    facts = node_facts(nodes, pods, metrics)
    ready_known = {
        obj["metadata"]["name"]
        for obj in nodes
        if any(
            c.get("type") == "Ready" and c.get("status") in ("True", "False")
            for c in obj.get("status", {}).get("conditions", [])
        )
    }
    facts = [fact if fact.name in ready_known else replace(fact, ready=None) for fact in facts]
    return cluster_view(
        ClusterFacts(
            observed_at=observed_at, connected=True, version=report["version"], nodes=facts
        )
    )


def _owner(obj: dict) -> dict:
    return next(
        (ref for ref in obj["metadata"].get("ownerReferences", []) if ref.get("controller")),
        {},
    )


def _pods_by_owner(scope: dict) -> dict:
    replicas = {
        item["metadata"]["uid"]: _owner(item)
        for item in scope["replica_sets"] or []
        if item["metadata"].get("uid")
    }
    grouped = {}
    for pod in scope["pods"] or []:
        owner = _owner(pod)
        if owner.get("kind") == "ReplicaSet":
            owner = replicas.get(owner.get("uid"), {})
        if owner.get("uid") and owner.get("kind") in OBSERVATION_KINDS:
            grouped.setdefault((owner["kind"], owner["uid"]), []).append(pod)
    return grouped


def observed_components(
    report: dict, baseline: list[tuple[str, ComponentView]]
) -> list[ComponentView]:
    covered = set()
    current = []
    for scope in report["namespaces"]:
        namespace = scope["name"]
        pods = _pods_by_owner(scope)
        for key, kinds in (
            ("workloads", OBSERVATION_KINDS - {"Rollout"}),
            ("rollouts", {"Rollout"}),
        ):
            if scope[key] is None:
                continue
            covered.update((namespace, kind) for kind in kinds)
            for item in scope[key]:
                kind = item["kind"]
                name = item["metadata"]["name"]
                ready, desired = replica_counts(kind.lower(), item)
                pod_list = None
                if scope["pods"] is not None and (
                    scope["replica_sets"] is not None or kind in ("StatefulSet", "DaemonSet")
                ):
                    pod_list = [
                        pod_view(pod_fact(obj)).model_copy(
                            update={
                                "restarts": sum(
                                    container.get("restartCount", 0)
                                    for key in ("initContainerStatuses", "containerStatuses")
                                    for container in obj.get("status", {}).get(key, [])
                                ),
                            }
                        )
                        for obj in sorted(
                            pods.get((kind, item["metadata"].get("uid")), []),
                            key=lambda obj: obj["metadata"]["name"],
                        )
                    ]
                status = (
                    "idle"
                    if desired == 0
                    else "healthy"
                    if ready >= desired
                    else "down"
                    if ready == 0
                    else "degraded"
                )
                current.append(
                    ComponentView(
                        id=component_id(namespace, kind, name),
                        name=name,
                        namespace=namespace,
                        placement=[f"ns {namespace}"],
                        layer="app",
                        kind="app",
                        managed=False,
                        status=status,
                        detail=f"{kind}, 준비 {ready}/{desired}",
                        pods=pod_list,
                    )
                )
    current.extend(view for kind, view in baseline if (view.namespace, kind) not in covered)
    return sorted(current, key=lambda view: (view.namespace or "", view.name, view.id))
