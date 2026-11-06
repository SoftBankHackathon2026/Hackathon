import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self, get_args

from controlplane.domain.fleet import ContainerFact, InventoryItem, PodFact
from controlplane.schemas.migrations import PodPhase

KubeObject = dict[str, Any]
KEPT_LABELS = (
    "app.kubernetes.io/name",
    "app.kubernetes.io/instance",
    "app.kubernetes.io/part-of",
    "app.kubernetes.io/component",
    "app",
)
HOST_TOKEN = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*")
SHORT_HOST = re.compile(
    r"(?:://|@)([a-z0-9][a-z0-9-]*)(?![a-z0-9.-])|(?<![a-z0-9.-])([a-z0-9][a-z0-9-]*):\d"
)
HOST_KEY = re.compile(r"HOST|ADDR|URL|URI|ENDPOINT|SERVER|BROKER|BOOTSTRAP", re.IGNORECASE)
EMBEDDED_PAIR = re.compile(
    r"""["']?([a-z_][a-z0-9_.-]*)["']?[ \t]*[:=][ \t]*["']?([a-z0-9][a-z0-9-]*)["']?[ \t]*(?=$|[,#}\s])""",
    re.MULTILINE,
)
SERVICE_DOMAINS = ("", ".svc", ".svc.cluster.local")
REGISTRY_KEYS = {"fleet.json"}
POD_OWNER_KINDS = {"ReplicaSet", "StatefulSet", "DaemonSet"}
POD_PHASES = set(get_args(PodPhase))


@dataclass(frozen=True)
class ConfigValue:
    key: str
    value: str

    def short_hosts(self) -> set[str]:
        value = self.value.strip().lower()
        names = {first or second for first, second in SHORT_HOST.findall(value)}
        names |= {host for key, host in EMBEDDED_PAIR.findall(value) if HOST_KEY.search(key)}
        if HOST_KEY.search(self.key) and HOST_TOKEN.fullmatch(value):
            names.add(value)
        return names


def field_at(obj: KubeObject, *path: str) -> Any:
    value: Any = obj
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def items_at(obj: KubeObject, *path: str) -> list[Any]:
    return field_at(obj, *path) or []


def mapping_at(obj: KubeObject, *path: str) -> dict[str, Any]:
    return field_at(obj, *path) or {}


def _plain_values(containers: list[KubeObject]) -> list[ConfigValue]:
    values = []
    for container in containers:
        for env in items_at(container, "env"):
            if env.get("value"):
                values.append(ConfigValue(env.get("name") or "", str(env["value"])))
        arguments = [*items_at(container, "command"), *items_at(container, "args")]
        values += [ConfigValue("", str(arg)) for arg in arguments]
    return values


def _configmap_names(pod_spec: KubeObject, containers: list[KubeObject]) -> list[str]:
    names = {field_at(volume, "configMap", "name") for volume in items_at(pod_spec, "volumes")}
    for container in containers:
        names |= {field_at(ref, "configMapRef", "name") for ref in items_at(container, "envFrom")}
        names |= {
            field_at(env, "valueFrom", "configMapKeyRef", "name")
            for env in items_at(container, "env")
        }
    return sorted(name for name in names if name)


def _replicas(kind: str, obj: KubeObject) -> tuple[int, int]:
    if kind == "daemonset":
        ready = field_at(obj, "status", "numberReady") or 0
        return ready, field_at(obj, "status", "desiredNumberScheduled") or 0
    replicas = field_at(obj, "spec", "replicas")
    ready = field_at(obj, "status", "readyReplicas") or 0
    return ready, 1 if replicas is None else replicas


def _pod_owner(obj: KubeObject) -> str | None:
    refs = items_at(obj, "metadata", "ownerReferences")
    owner = next((ref for ref in refs if ref.get("controller")), None)
    if owner is None or owner.get("kind") not in POD_OWNER_KINDS:
        return None
    name = owner.get("name") or ""
    if owner["kind"] == "ReplicaSet":
        name = name.rsplit("-", 1)[0]
    return f"{field_at(obj, 'metadata', 'namespace') or ''}/{name}"


def _pod_reason(obj: KubeObject) -> str | None:
    if field_at(obj, "metadata", "deletionTimestamp"):
        return "Terminating"
    statuses = [
        *items_at(obj, "status", "initContainerStatuses"),
        *items_at(obj, "status", "containerStatuses"),
    ]
    for status in statuses:
        state = mapping_at(status, "state")
        reason = field_at(state, "waiting", "reason") or field_at(state, "terminated", "reason")
        if reason and reason != "Completed":
            return reason
    return field_at(obj, "status", "reason")


def _pod_ready(obj: KubeObject) -> bool:
    conditions = items_at(obj, "status", "conditions")
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)


def _containers(obj: KubeObject) -> list[ContainerFact]:
    statuses = {s.get("name"): s for s in items_at(obj, "status", "containerStatuses")}
    containers = []
    for spec in items_at(obj, "spec", "containers"):
        status = statuses.get(spec.get("name"), {})
        containers.append(
            ContainerFact(
                name=spec.get("name") or "",
                ready=bool(status.get("ready")),
                restarts=int(status.get("restartCount") or 0),
                image=spec.get("image") or "",
            )
        )
    return containers


def pod_fact(obj: KubeObject) -> PodFact:
    started = field_at(obj, "status", "startTime")
    phase = field_at(obj, "status", "phase")
    return PodFact(
        name=field_at(obj, "metadata", "name") or "",
        phase=phase if phase in POD_PHASES else "Unknown",
        ready=_pod_ready(obj),
        containers=_containers(obj),
        node=field_at(obj, "spec", "nodeName"),
        reason=_pod_reason(obj),
        started_at=datetime.fromisoformat(started) if started else None,
    )


def pods_by_workload(pods: list[KubeObject]) -> dict[str, list[PodFact]]:
    grouped: dict[str, list[PodFact]] = {}
    for obj in pods:
        owner = _pod_owner(obj)
        if owner is not None:
            grouped.setdefault(owner, []).append(pod_fact(obj))
    return grouped


@dataclass(frozen=True)
class WorkloadSpec:
    namespace: str
    kind: str
    name: str
    ready: int
    desired: int
    images: list[str]
    labels: dict[str, str]
    pod_labels: dict[str, str]
    config_values: list[ConfigValue]
    configmaps: list[str]

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.name}"

    @classmethod
    def from_k8s(cls, kind: str, obj: KubeObject) -> Self:
        pod_spec = mapping_at(obj, "spec", "template", "spec")
        containers = [*items_at(pod_spec, "initContainers"), *items_at(pod_spec, "containers")]
        labels = mapping_at(obj, "metadata", "labels")
        ready, desired = _replicas(kind, obj)
        return cls(
            namespace=field_at(obj, "metadata", "namespace") or "",
            kind=kind,
            name=field_at(obj, "metadata", "name") or "",
            ready=int(ready),
            desired=int(desired),
            images=[c.get("image") or "" for c in items_at(pod_spec, "containers")],
            labels={k: labels[k] for k in KEPT_LABELS if k in labels},
            pod_labels=mapping_at(obj, "spec", "template", "metadata", "labels"),
            config_values=_plain_values(containers),
            configmaps=_configmap_names(pod_spec, containers),
        )


@dataclass(frozen=True)
class ServiceSpec:
    namespace: str
    name: str
    selector: dict[str, str]

    @classmethod
    def from_k8s(cls, obj: KubeObject) -> Self:
        return cls(
            namespace=field_at(obj, "metadata", "namespace") or "",
            name=field_at(obj, "metadata", "name") or "",
            selector=mapping_at(obj, "spec", "selector"),
        )

    def selects(self, workload: WorkloadSpec) -> bool:
        if not self.selector or workload.namespace != self.namespace:
            return False
        return all(workload.pod_labels.get(k) == v for k, v in self.selector.items())

    @property
    def hosts(self) -> list[str]:
        return [f"{self.name}.{self.namespace}{domain}" for domain in SERVICE_DOMAINS]


@dataclass(frozen=True)
class IngressSpec:
    namespace: str
    ingress_class: str
    services: list[str]

    @classmethod
    def from_k8s(cls, obj: KubeObject) -> Self:
        backends = [field_at(obj, "spec", "defaultBackend")]
        for rule in items_at(obj, "spec", "rules"):
            backends += [path.get("backend") for path in items_at(rule, "http", "paths")]
        names = [field_at(backend or {}, "service", "name") for backend in backends]
        ingress_class = field_at(obj, "spec", "ingressClassName") or field_at(
            obj, "metadata", "annotations", "kubernetes.io/ingress.class"
        )
        return cls(
            namespace=field_at(obj, "metadata", "namespace") or "",
            ingress_class=ingress_class or "",
            services=[name for name in names if name],
        )


@dataclass(frozen=True)
class ClusterObjects:
    workloads: list[WorkloadSpec]
    services: list[ServiceSpec]
    ingresses: list[IngressSpec]
    configmaps: dict[tuple[str, str], list[ConfigValue]]
    pods: dict[str, list[PodFact]] | None = None

    def pods_of(self, workload: WorkloadSpec) -> list[PodFact] | None:
        return None if self.pods is None else self.pods.get(workload.key, [])

    def values_of(self, workload: WorkloadSpec) -> list[ConfigValue]:
        mounted = [
            value
            for name in workload.configmaps
            for value in self.configmaps.get((workload.namespace, name), [])
        ]
        return [v for v in [*workload.config_values, *mounted] if v.key not in REGISTRY_KEYS]


@dataclass(frozen=True)
class ServiceIndex:
    by_name: dict[tuple[str, str], list[str]]
    by_host: dict[str, list[str]]

    @classmethod
    def build(cls, objects: ClusterObjects) -> Self:
        by_name: dict[tuple[str, str], list[str]] = {}
        by_host: dict[str, list[str]] = {}
        for service in objects.services:
            keys = [w.key for w in objects.workloads if service.selects(w)]
            if not keys:
                continue
            by_name[(service.namespace, service.name)] = keys
            for host in service.hosts:
                by_host.setdefault(host, []).extend(keys)
        return cls(by_name=by_name, by_host=by_host)


def _ingress_classes(objects: ClusterObjects, index: ServiceIndex) -> dict[str, set[str]]:
    classes: dict[str, set[str]] = {}
    for ingress in objects.ingresses:
        for service in ingress.services:
            for key in index.by_name.get((ingress.namespace, service), []):
                classes.setdefault(key, set()).add(ingress.ingress_class)
    return classes


def _calls(workload: WorkloadSpec, values: list[ConfigValue], index: ServiceIndex) -> list[str]:
    found: set[str] = set()
    for item in values:
        for host in HOST_TOKEN.findall(item.value.lower()):
            found.update(index.by_host.get(host, []))
        for name in item.short_hosts():
            found.update(index.by_name.get((workload.namespace, name), []))
    return sorted(found)


def _mentions(values: list[ConfigValue], parts: list[str]) -> bool:
    lowered = [item.value.lower() for item in values]
    return any(part in value for part in parts for value in lowered)


def _external_calls(values: list[ConfigValue], hints: dict[str, list[str]]) -> list[str]:
    return sorted(component for component, parts in hints.items() if _mentions(values, parts))


def build_inventory(objects: ClusterObjects, hints: dict[str, list[str]]) -> list[InventoryItem]:
    index = ServiceIndex.build(objects)
    classes = _ingress_classes(objects, index)
    items = []
    for workload in objects.workloads:
        values = objects.values_of(workload)
        items.append(
            InventoryItem(
                namespace=workload.namespace,
                kind=workload.kind,
                name=workload.name,
                ready=workload.ready,
                desired=workload.desired,
                images=workload.images,
                labels=workload.labels,
                uses=_calls(workload, values, index),
                uses_external=_external_calls(values, hints),
                ingress_classes=sorted(classes.get(workload.key, set())),
                pods=objects.pods_of(workload),
            )
        )
    return items
