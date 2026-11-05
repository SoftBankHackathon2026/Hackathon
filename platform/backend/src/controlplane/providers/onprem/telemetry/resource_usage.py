from datetime import UTC, datetime, timedelta

from kubernetes.utils.quantity import parse_quantity

from controlplane.providers.telemetry import ResourceUsage

APP_CONTAINER = "app"
MAX_SAMPLE_AGE = timedelta(minutes=5)
METRICS_TIMEOUT = (3, 5)


def _millicores(quantity: str) -> float:
    return float(parse_quantity(quantity)) * 1000


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _running_pod_names(pods: list) -> set[str]:
    return {p.metadata.name for p in pods if p.status.phase == "Running"}


def _app_limits(pods: list, wanted: set[str]) -> list[dict]:
    limits = []
    for pod in pods:
        if pod.metadata.name not in wanted:
            continue
        for container in pod.spec.containers:
            if container.name == APP_CONTAINER:
                limits.append(container.resources.limits or {} if container.resources else {})
    return limits


def _sum_limit(limits: list[dict], key: str, wanted: set[str], convert) -> float | int | None:
    if len(limits) != len(wanted) or not all(v.get(key) for v in limits):
        return None
    return sum(convert(v[key]) for v in limits)


def _percent(used: float, limit: float | int | None) -> float | None:
    return round(used / limit * 100, 2) if limit else None


def read_resource_usage(clients, namespace: str, pods: list) -> ResourceUsage | None:
    wanted = _running_pod_names(pods)
    try:
        data = clients.custom.list_namespaced_custom_object(
            "metrics.k8s.io", "v1beta1", namespace, "pods", _request_timeout=METRICS_TIMEOUT
        )
        cpu = memory = 0
        seen = set()
        timestamps = []
        for pod in data.get("items", []):
            if pod["metadata"]["name"] not in wanted:
                continue
            for container in pod["containers"]:
                if container["name"] != APP_CONTAINER:
                    continue
                seen.add(pod["metadata"]["name"])
                cpu += _millicores(container["usage"]["cpu"])
                memory += int(parse_quantity(container["usage"]["memory"]))
                if pod.get("timestamp"):
                    timestamps.append(_parse_timestamp(pod["timestamp"]))
        if seen != wanted or not wanted:
            return None
        sampled_at = min(timestamps) if len(timestamps) == len(wanted) else None
        if sampled_at and sampled_at < datetime.now(UTC) - MAX_SAMPLE_AGE:
            return None
        limits = _app_limits(pods, wanted)
        cpu_limit = _sum_limit(limits, "cpu", wanted, _millicores)
        memory_limit = _sum_limit(limits, "memory", wanted, lambda v: int(parse_quantity(v)))
        return ResourceUsage(
            cpu_millicores=round(cpu, 1),
            memory_bytes=memory,
            pods=len(seen),
            cpu_percent=_percent(cpu, cpu_limit),
            memory_percent=_percent(memory, memory_limit),
            cpu_limit_millicores=cpu_limit,
            memory_limit_bytes=memory_limit,
            cpu_sampled_at=sampled_at,
            memory_sampled_at=sampled_at,
            scope="app_containers",
        )
    except Exception:
        return None
