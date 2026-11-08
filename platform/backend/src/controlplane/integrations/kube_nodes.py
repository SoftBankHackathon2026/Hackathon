import re
from decimal import Decimal

from controlplane.domain.fleet import NodeFact, Resources
from controlplane.integrations.kube_inventory import KubeObject, field_at, items_at, mapping_at

QUANTITY = re.compile(r"^([+-]?[0-9.]+(?:[eE][+-]?[0-9]+)?)([a-zA-Z]*)$")
SUFFIXES = {
    "": Decimal(1),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
    "Ki": Decimal(2**10),
    "Mi": Decimal(2**20),
    "Gi": Decimal(2**30),
    "Ti": Decimal(2**40),
    "Pi": Decimal(2**50),
    "Ei": Decimal(2**60),
}
ROLE_PREFIX = "node-role.kubernetes.io/"
FINISHED_PHASES = {"Succeeded", "Failed"}


def quantity(value: str | None) -> Decimal:
    match = QUANTITY.match(str(value or "0").strip())
    if match is None or match.group(2) not in SUFFIXES:
        return Decimal(0)
    return Decimal(match.group(1)) * SUFFIXES[match.group(2)]


def resources_of(mapping: dict[str, str]) -> Resources:
    return Resources(
        cpu_millicores=int(quantity(mapping.get("cpu")) * 1000),
        memory_bytes=int(quantity(mapping.get("memory"))),
    )


def _add(left: Resources, right: Resources) -> Resources:
    return Resources(
        left.cpu_millicores + right.cpu_millicores, left.memory_bytes + right.memory_bytes
    )


def _larger(left: Resources, right: Resources) -> Resources:
    return Resources(
        max(left.cpu_millicores, right.cpu_millicores), max(left.memory_bytes, right.memory_bytes)
    )


def pod_requests(pod: KubeObject) -> Resources:
    def requested(container: KubeObject) -> Resources:
        return resources_of(mapping_at(container, "resources", "requests"))

    running = Resources(0, 0)
    for container in items_at(pod, "spec", "containers"):
        running = _add(running, requested(container))
    sidecars = Resources(0, 0)
    init_peak = Resources(0, 0)
    for container in items_at(pod, "spec", "initContainers"):
        resources = requested(container)
        if container.get("restartPolicy") == "Always":
            sidecars = _add(sidecars, resources)
            init_peak = _larger(init_peak, sidecars)
        else:
            init_peak = _larger(init_peak, _add(sidecars, resources))
    return _add(
        _larger(_add(running, sidecars), init_peak),
        resources_of(mapping_at(pod, "spec", "overhead")),
    )


def load_by_node(pods: list[KubeObject]) -> dict[str, tuple[Resources, int]]:
    load: dict[str, tuple[Resources, int]] = {}
    for pod in pods:
        node = field_at(pod, "spec", "nodeName")
        if not node or field_at(pod, "status", "phase") in FINISHED_PHASES:
            continue
        total, count = load.get(node, (Resources(0, 0), 0))
        load[node] = (_add(total, pod_requests(pod)), count + 1)
    return load


def usage_by_node(metrics: list[KubeObject]) -> dict[str, Resources]:
    return {
        field_at(item, "metadata", "name"): resources_of(mapping_at(item, "usage"))
        for item in metrics
    }


def _described(obj: KubeObject) -> dict[str, object]:
    labels = mapping_at(obj, "metadata", "labels")
    conditions = items_at(obj, "status", "conditions")
    return {
        "ready": any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions),
        "unschedulable": bool(field_at(obj, "spec", "unschedulable")),
        "roles": sorted(k.removeprefix(ROLE_PREFIX) for k in labels if k.startswith(ROLE_PREFIX)),
        "kubelet_version": field_at(obj, "status", "nodeInfo", "kubeletVersion"),
        "os_image": field_at(obj, "status", "nodeInfo", "osImage"),
        "arch": field_at(obj, "status", "nodeInfo", "architecture"),
        "instance_type": labels.get("node.kubernetes.io/instance-type"),
        "zone": labels.get("topology.kubernetes.io/zone"),
        "allocatable": resources_of(mapping_at(obj, "status", "allocatable")),
    }


def node_facts(
    nodes: list[KubeObject] | None,
    pods: list[KubeObject] | None,
    metrics: list[KubeObject] | None,
) -> list[NodeFact] | None:
    described = {field_at(n, "metadata", "name"): _described(n) for n in nodes or []}
    load = None if pods is None else load_by_node(pods)
    usage = None if metrics is None else usage_by_node(metrics)
    names = {*described, *(load or {}), *(usage or {})}
    if nodes is None and not names:
        return None
    facts = []
    for name in sorted(names):
        requests, pod_count = (load or {}).get(name, (Resources(0, 0), 0))
        facts.append(
            NodeFact(
                name=name,
                **described.get(name, {}),
                requests=None if load is None else requests,
                pods=None if load is None else pod_count,
                usage=None if usage is None else usage.get(name),
            )
        )
    return facts
