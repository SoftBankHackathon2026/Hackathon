"""Traffic of one app for the platform's monitoring view.

Read-only: ALB target group metrics and ECS service metrics (CloudWatch). Traces
and trace logs live in traces.py.
"""

from datetime import UTC, datetime, timedelta

from controlplane.providers.contracts import ProviderRejected, ProviderUnavailable
from controlplane.providers.telemetry import (
    ResourceUsage,
    Traffic,
    TrafficPoint,
    TrafficSeries,
    latest_resource_sample,
)
from oneport_aws.providers.oneport_ecs.gateways.base import Payload
from oneport_aws.providers.oneport_ecs.gateways.cloudwatch import metric_query
from oneport_aws.providers.oneport_ecs.gateways.elb import target_group_dimension
from oneport_aws.providers.oneport_ecs.provider import OneportEcsProvider
from oneport_aws.providers.oneport_ecs.routing import COLORS
from oneport_aws.providers.oneport_ecs.status import TrafficWeight

SHORT_PERIOD_SECONDS = 60
LONG_PERIOD_SECONDS = 300
SHORT_PERIOD_MAX_MINUTES = 180

_METRICS = {
    "requests": ("RequestCount", "Sum"),
    "server_errors": ("HTTPCode_Target_5XX_Count", "Sum"),
    "client_errors": ("HTTPCode_Target_4XX_Count", "Sum"),
    "p95": ("TargetResponseTime", "p95"),
    "healthy": ("HealthyHostCount", "Maximum"),
}
_RESOURCE_METRICS = (("cpu", "CPUUtilization"), ("memory", "MemoryUtilization"))

MetricValues = dict[str, dict[datetime, float]]


def _floor(at: datetime, period: int) -> datetime:
    return datetime.fromtimestamp(int(at.timestamp()) // period * period, UTC)


def _target_groups(provider: OneportEcsProvider) -> dict[str, str]:
    env, found = provider.environment, {}
    for color in COLORS:
        group = provider.gateways.elb.find_target_group(env.target_group_name(color))
        if group:
            found[color] = target_group_dimension(group.arn)
    return found


def _resources(provider: OneportEcsProvider, now: datetime) -> ResourceUsage | None:
    env = provider.environment
    dimensions = {
        "ClusterName": env.cluster.rsplit("/", 1)[-1],
        "ServiceName": env.service.rsplit("/", 1)[-1],
    }
    queries = [
        metric_query(
            key,
            namespace="AWS/ECS",
            metric=metric,
            dimensions=dimensions,
            period=60,
            stat="Average",
        )
        for key, metric in _RESOURCE_METRICS
    ]
    try:
        values = provider.gateways.cloudwatch.get_metric_values(
            queries, now - timedelta(minutes=10), now
        )
    except (ProviderRejected, ProviderUnavailable):
        return None
    cpu, cpu_at = latest_resource_sample(values.get("cpu", {}), now)
    memory, memory_at = latest_resource_sample(values.get("memory", {}), now)
    if cpu is None and memory is None:
        return None
    return ResourceUsage(
        scope="service",
        sample_window_seconds=60,
        cpu_percent=cpu,
        cpu_sampled_at=cpu_at,
        memory_percent=memory,
        memory_sampled_at=memory_at,
    )


def _traffic_queries(load_balancer: str, groups: dict[str, str], period: int) -> list[Payload]:
    return [
        metric_query(
            f"{color}_{key}",
            namespace="AWS/ApplicationELB",
            metric=metric,
            dimensions={"TargetGroup": group, "LoadBalancer": load_balancer},
            period=period,
            stat=stat,
        )
        for color, group in groups.items()
        for key, (metric, stat) in _METRICS.items()
    ]


def _point(values: MetricValues, color: str, when: datetime) -> TrafficPoint:
    def at(key: str) -> float | None:
        return values.get(f"{color}_{key}", {}).get(when)

    p95 = at("p95")
    return TrafficPoint(
        at=when,
        requests=at("requests") or 0.0,
        server_errors=at("server_errors") or 0.0,
        client_errors=at("client_errors") or 0.0,
        p95_ms=None if p95 is None else round(p95 * 1000, 1),
        healthy=at("healthy"),
    )


def _series(
    color: str,
    group: str,
    values: MetricValues,
    grid: list[datetime],
    weights: dict[str, TrafficWeight],
) -> TrafficSeries:
    name = group.split("/")[1]
    current = weights.get(name)
    return TrafficSeries(
        color=color,
        target_group=name,
        weight=current.weight if current else 0,
        healthy_targets=current.healthy if current else None,
        points=[_point(values, color, when) for when in grid],
    )


def traffic(provider: OneportEcsProvider, *, minutes: int) -> Traffic:
    env = provider.environment
    period = SHORT_PERIOD_SECONDS if minutes <= SHORT_PERIOD_MAX_MINUTES else LONG_PERIOD_SECONDS
    end = _floor(datetime.now(UTC), period)
    start = end - timedelta(minutes=minutes)
    groups = _target_groups(provider)
    queries = _traffic_queries(env.load_balancer_dimension, groups, period)
    values = provider.gateways.cloudwatch.get_metric_values(queries, start, end) if queries else {}
    grid = [start + timedelta(seconds=period * i) for i in range(minutes * 60 // period)]
    status = provider.status()
    deployment = status.deployment
    return Traffic(
        application=env.application,
        environment_id=env.environment_id,
        minutes=minutes,
        period_seconds=period,
        live_release=status.release,
        running=status.running,
        desired=status.desired,
        deployment_status=deployment.status if deployment else None,
        deployment_stage=deployment.stage if deployment else None,
        resource_usage=_resources(provider, datetime.now(UTC)),
        series=[
            _series(color, group, values, grid, status.traffic) for color, group in groups.items()
        ],
    )
