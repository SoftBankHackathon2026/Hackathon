"""Bounded, read-only Kubernetes observations for a registered target."""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from .collector import InventoryError, Kubectl, _items
from .schema import ReportError, canonical_json, exact, text

# Leave room for the signed device envelope in the 256 KiB relay limit.
REPORT_BYTES = 200_000
MAX_NAMESPACES = 16
OBSERVE = "environment.observe"
OBSERVE_INTERVAL_MS = 60_000
OBSERVE_FRESH_MS = 3 * OBSERVE_INTERVAL_MS
NAMESPACE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
QUANTITY = re.compile(
    r"[+-]?(?:[0-9]{1,20}(?:\.[0-9]{0,20})?|\.[0-9]{1,20})"
    r"(?:[eE][+-]?[0-9]{1,2}|[numkMGTPE]|[KMGTPE]i)?"
)

NAME = {"name": str}
OWNER = {"uid": str, "kind": str, "controller": bool}
OBJECT = {**NAME, "uid": str, "ownerReferences": [OWNER]}
RESOURCES = {"cpu": QUANTITY, "memory": QUANTITY}
CONDITION = {"type": str, "status": str}
CONTAINER = {**NAME, "image": str, "resources": {"requests": RESOURCES}}
REASON = {"reason": str}
CONTAINER_STATUS = {
    **NAME,
    "ready": bool,
    "restartCount": int,
    "state": {"waiting": REASON, "terminated": REASON},
}
NODE = {
    "metadata": {
        **NAME,
        "labels": {
            "node-role.kubernetes.io/control-plane": str,
            "node-role.kubernetes.io/master": str,
            "node-role.kubernetes.io/worker": str,
            "node.kubernetes.io/instance-type": str,
            "topology.kubernetes.io/zone": str,
        },
    },
    "spec": {"unschedulable": bool},
    "status": {
        "conditions": [CONDITION],
        "allocatable": RESOURCES,
        "nodeInfo": {"kubeletVersion": str, "osImage": str, "architecture": str},
    },
}
POD = {
    "metadata": {**OBJECT, "deletionTimestamp": str},
    "spec": {
        "nodeName": str,
        "containers": [CONTAINER],
        "initContainers": [{**CONTAINER, "restartPolicy": str}],
        "overhead": RESOURCES,
    },
    "status": {
        "phase": str,
        "conditions": [CONDITION],
        "reason": str,
        "containerStatuses": [CONTAINER_STATUS],
        "initContainerStatuses": [CONTAINER_STATUS],
    },
}
WORKLOAD = {
    "kind": str,
    "metadata": OBJECT,
    "spec": {"replicas": int},
    "status": {"readyReplicas": int, "numberReady": int, "desiredNumberScheduled": int},
}
REPLICA_SET = {"metadata": OBJECT}
METRIC = {"metadata": NAME, "usage": RESOURCES}
NAMESPACE = {
    "workloads": WORKLOAD,
    "rollouts": WORKLOAD,
    "replica_sets": REPLICA_SET,
    "pods": POD,
}
WORKLOAD_KINDS = frozenset(("Deployment", "StatefulSet", "DaemonSet", "Rollout"))


def _project(value, shape):
    if isinstance(shape, dict):
        if not isinstance(value, dict):
            raise ReportError("INVALID_OBSERVATION")
        return {
            key: _project(value[key], spec)
            for key, spec in shape.items()
            if key in value
        }
    if isinstance(shape, list):
        if not isinstance(value, list):
            raise ReportError("INVALID_OBSERVATION")
        return [_project(item, shape[0]) for item in value]
    if shape is QUANTITY:
        if not isinstance(value, str) or not QUANTITY.fullmatch(value):
            raise ReportError("INVALID_OBSERVATION_QUANTITY")
        return value
    if type(value) is not shape or (shape is int and value < 0):
        raise ReportError("INVALID_OBSERVATION")
    if shape is str:
        return text(value, empty=True, limit=512)
    return value


def _objects(values, shape):
    projected = _project(values, [shape])
    if any(not obj.get("metadata", {}).get("name") for obj in projected):
        raise ReportError("INVALID_OBSERVATION")
    if shape is WORKLOAD and any(
        obj.get("kind") not in WORKLOAD_KINDS for obj in projected
    ):
        raise ReportError("INVALID_OBSERVATION")
    return projected


def validate_observation(report, namespaces, namespace_uid):
    exact(report, {"namespace_uid", "version", "nodes", "metrics", "namespaces"})
    if report["namespace_uid"] != namespace_uid:
        raise ReportError("CLUSTER_IDENTITY_UNVERIFIED")
    text(report["version"])
    scopes = report["namespaces"]
    if not isinstance(scopes, list) or [item["name"] for item in scopes] != namespaces:
        raise ReportError("INVALID_OBSERVATION_SCOPE")

    def check(values, shape):
        if values is not None and _objects(values, shape) != values:
            raise ReportError("INVALID_OBSERVATION_FIELDS")

    check(report["nodes"], NODE)
    check(report["metrics"], METRIC)
    for scope in scopes:
        exact(scope, {"name", *NAMESPACE})
        for key, shape in NAMESPACE.items():
            check(scope[key], shape)
        if any(item["kind"] == "Rollout" for item in scope["workloads"] or []) or any(
            item["kind"] != "Rollout" for item in scope["rollouts"] or []
        ):
            raise ReportError("INVALID_OBSERVATION_FIELDS")
    if len(canonical_json(report)) > REPORT_BYTES:
        raise ReportError("OBSERVATION_TOO_LARGE")


def observe(
    kubectl: Kubectl, *, target_id: str, namespace_uid: str, namespaces: list[str]
) -> dict:
    if len(namespaces) > MAX_NAMESPACES or any(
        not NAMESPACE_NAME.fullmatch(name) for name in namespaces
    ):
        raise InventoryError("INVALID_OBSERVATION_SCOPE")
    selected = next(
        (row for row in kubectl.descriptors() if row["id"] == target_id), None
    )
    if selected is None:
        raise InventoryError("SELECTED_TARGET_CHANGED_OR_MISSING")
    context = selected["context"]

    def read(*args):
        return kubectl.json([*args, "--output=json"], context=context)

    def verify_identity():
        if read("get", "namespace", "kube-system")["metadata"]["uid"] != namespace_uid:
            raise InventoryError("CLUSTER_IDENTITY_UNVERIFIED")

    verify_identity()
    version = read("version")["serverVersion"]["gitVersion"]

    def read_objects(shape, *args):
        try:
            items = _items(
                kubectl.json(list(args), context=context)
                if shape is METRIC
                else read(*args)
            )
            if shape is METRIC:
                now = datetime.now(UTC)

                def fresh(item):
                    try:
                        age = (
                            now - datetime.fromisoformat(item["timestamp"])
                        ).total_seconds()
                        return -15 <= age <= 120
                    except (ValueError, KeyError, TypeError):
                        return False

                items = [item for item in items if fresh(item)]
            values = _objects(items, shape)
            return values if len(canonical_json(values)) <= REPORT_BYTES else None
        except (InventoryError, ReportError, KeyError, TypeError):
            return None

    resource_names = {
        "workloads": "deployments.apps,statefulsets.apps,daemonsets.apps",
        "rollouts": "rollouts.argoproj.io",
        "replica_sets": "replicasets.apps",
        "pods": "pods",
    }
    with ThreadPoolExecutor(max_workers=12) as workers:
        nodes = workers.submit(read_objects, NODE, "get", "nodes")
        metrics = workers.submit(
            read_objects, METRIC, "get", "--raw=/apis/metrics.k8s.io/v1beta1/nodes"
        )
        scopes = [
            {
                key: workers.submit(
                    read_objects, shape, "get", resource_names[key], "-n", name
                )
                for key, shape in NAMESPACE.items()
            }
            for name in namespaces
        ]
        report = {
            "namespace_uid": namespace_uid,
            "version": version,
            "nodes": nodes.result(),
            "metrics": metrics.result(),
            "namespaces": [
                {"name": name, **dict.fromkeys(NAMESPACE)} for name in namespaces
            ],
        }
        if len(canonical_json(report)) > REPORT_BYTES:
            report["metrics"] = None
        if len(canonical_json(report)) > REPORT_BYTES:
            report["nodes"] = None
        for scope, jobs in zip(report["namespaces"], scopes, strict=True):
            for key, job in jobs.items():
                scope[key] = job.result()
                if len(canonical_json(report)) > REPORT_BYTES:
                    scope[key] = None

    verify_identity()
    if selected not in kubectl.descriptors():
        raise InventoryError("SELECTED_TARGET_CHANGED_OR_MISSING")
    validate_observation(report, namespaces, namespace_uid)
    return report
