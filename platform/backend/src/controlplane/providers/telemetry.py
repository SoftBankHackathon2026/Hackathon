"""Optional monitoring port: traffic, traces and trace logs read from the provider.

Values are read on request and never stored in the platform DB.
"""

from datetime import datetime, timedelta
from math import isfinite
from typing import Literal, Protocol, runtime_checkable

from controlplane.domain.models import Model
from controlplane.providers.interface import AppLogLine

TraceKind = Literal["all", "errors", "slow"]


class TrafficPoint(Model):
    at: datetime
    requests: float
    server_errors: float
    client_errors: float
    p95_ms: float | None = None
    healthy: float | None = None


class TrafficSeries(Model):
    color: str
    label: str | None = None
    target_group: str
    weight: int
    healthy_targets: int | None = None
    points: list[TrafficPoint]


class ResourceUsage(Model):
    cpu_millicores: float | None = None
    memory_bytes: int | None = None
    pods: int | None = None
    cpu_percent: float | None = None
    memory_percent: float | None = None
    cpu_limit_millicores: float | None = None
    memory_limit_bytes: int | None = None
    cpu_sampled_at: datetime | None = None
    memory_sampled_at: datetime | None = None
    sample_window_seconds: int | None = None
    scope: Literal["app_containers", "service"] = "service"


def latest_resource_sample(values: dict[datetime, float], now: datetime):
    """A missing/stopped service is unknown, never zero or an old busy sample."""
    usable = [
        (at, value)
        for at, value in values.items()
        if now - timedelta(minutes=5) <= at <= now and isfinite(value) and value >= 0
    ]
    if not usable:
        return None, None
    at, value = max(usable)
    return round(value, 2), at


class Traffic(Model):
    application: str
    environment_id: str
    minutes: int
    period_seconds: int
    source: Literal["cloudwatch", "prometheus", "cloud_monitoring"] = "cloudwatch"
    resource_usage: ResourceUsage | None = None
    live_release: str | None = None
    running: int | None = None
    running_sampled_at: datetime | None = None
    desired: int | None = None
    deployment_status: str | None = None
    deployment_stage: str | None = None
    series: list[TrafficSeries]


class TraceSummary(Model):
    id: str
    started_at: datetime
    duration_ms: float
    method: str | None = None
    path: str | None = None
    status: int | None = None
    error: bool = False
    fault: bool = False
    throttle: bool = False


class TraceList(Model):
    application: str
    environment_id: str
    minutes: int
    kind: TraceKind
    tracing_enabled: bool
    traces: list[TraceSummary]


class TraceSpan(Model):
    id: str
    parent_id: str | None = None
    name: str
    kind: Literal["segment", "subsegment"]
    namespace: str | None = None
    depth: int
    start_ms: float
    duration_ms: float
    in_progress: bool = False
    error: bool = False
    fault: bool = False
    throttle: bool = False
    http_method: str | None = None
    http_path: str | None = None
    http_status: int | None = None
    sql: str | None = None
    exception: str | None = None


class TraceDetail(Model):
    id: str
    application: str
    environment_id: str
    started_at: datetime
    duration_ms: float
    release: str | None = None
    spans: list[TraceSpan]
    logs: list[AppLogLine]


@runtime_checkable
class TelemetryProvider(Protocol):
    def traffic(self, *, minutes: int) -> Traffic: ...
    def traces(self, *, minutes: int, kind: TraceKind) -> TraceList: ...
    def trace(self, trace_id: str) -> TraceDetail | None: ...
