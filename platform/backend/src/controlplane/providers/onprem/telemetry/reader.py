import re
from datetime import timedelta

from controlplane.core.errors import UnsupportedOperation
from controlplane.providers.interface import AppLogs
from controlplane.providers.onprem.connection import connect
from controlplane.providers.onprem.log_store import stored_logs, uses_loki
from controlplane.providers.onprem.manifests import HASH
from controlplane.providers.onprem.observability import TelemetryBackend, TelemetryProxy
from controlplane.providers.onprem.pods import RELEASE
from controlplane.providers.onprem.telemetry.pod_logs import live_pod_logs
from controlplane.providers.onprem.telemetry.resource_usage import read_resource_usage
from controlplane.providers.onprem.telemetry.snapshot import RolloutSnapshot, read_snapshot
from controlplane.providers.onprem.telemetry.trace_detail import trace_detail
from controlplane.providers.onprem.telemetry.traces import TempoTraceSearch
from controlplane.providers.onprem.telemetry.traffic import (
    PrometheusTraffic,
    TrafficWindow,
    traffic_groups,
)
from controlplane.providers.telemetry import TraceDetail, TraceList, Traffic

TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
NOT_CONNECTED = "이 환경에는 온프렘 관측 저장소가 연결되지 않았습니다."
TRACE_LOG_MINUTES = 30
TRACE_LOG_LIMIT = 200
TRACE_LOG_PADDING = timedelta(seconds=30)


class OnpremTelemetry:
    def __init__(self, settings, rollout_name, *, clients=None, proxy=None):
        self.settings, self.env, self.name = settings, settings.environment, rollout_name
        self.clients, self.proxy = clients, proxy
        self._owned = clients is None

    def __enter__(self):
        if self.clients is None:
            self.clients = connect(
                kubeconfig=self.settings.kubeconfig,
                context=self.settings.context,
                expected_server=self.settings.server,
            )
        if self.proxy is None and self.settings.observability:
            self.proxy = TelemetryProxy(self.clients, self.settings.observability)
        return self

    def __exit__(self, *_):
        if self._owned and self.clients:
            self.clients.close()

    def _require_proxy(self) -> None:
        if self.proxy is None:
            raise UnsupportedOperation(NOT_CONNECTED)

    def _snapshot(self) -> RolloutSnapshot:
        return read_snapshot(self.clients, self.env, self.name)

    def app_logs(self, *, minutes, filter_text, limit, start=None, end=None) -> AppLogs:
        if uses_loki(self.settings):
            self._require_proxy()
            return stored_logs(
                self.proxy,
                self.env,
                minutes=minutes,
                filter_text=filter_text,
                limit=limit,
                start=start,
                end=end,
            )
        snapshot = self._snapshot()
        return live_pod_logs(
            self.clients,
            self.env,
            snapshot.pods,
            minutes=minutes,
            filter_text=filter_text,
            limit=limit,
            start=start,
            end=end,
        )

    def resource_usage(self, pods):
        return read_resource_usage(self.clients, self.env.namespace, pods)

    def traffic(self, *, minutes: int) -> Traffic:
        self._require_proxy()
        snapshot = self._snapshot()
        window = TrafficWindow(minutes)
        rollout = snapshot.rollout
        if rollout is None:
            return Traffic(
                application=self.env.application,
                environment_id=self.env.environment_id,
                minutes=minutes,
                period_seconds=window.period,
                source="prometheus",
                series=[],
            )
        groups = traffic_groups(rollout)
        series = PrometheusTraffic(self.proxy, self.env.namespace).read_series(groups, window)
        status = rollout.get("status", {})
        return Traffic(
            application=self.env.application,
            environment_id=self.env.environment_id,
            minutes=minutes,
            period_seconds=window.period,
            source="prometheus",
            series=series,
            live_release=self._live_release(status, snapshot.pods),
            running=status.get("readyReplicas", 0),
            desired=rollout["spec"].get("replicas", 1),
            deployment_status=status.get("phase"),
            resource_usage=self.resource_usage(snapshot.pods),
        )

    @staticmethod
    def _live_release(status: dict, pods: list) -> str | None:
        active = status.get("blueGreen", {}).get("activeSelector") or status.get("stableRS")
        for pod in pods:
            if pod.metadata.labels.get(HASH) == active:
                return pod.metadata.labels.get(RELEASE)
        return None

    def traces(self, *, minutes: int, kind: str) -> TraceList:
        self._require_proxy()
        search = TempoTraceSearch(self.proxy, self.env.application, self.env.environment_id)
        return TraceList(
            application=self.env.application,
            environment_id=self.env.environment_id,
            minutes=minutes,
            kind=kind,
            tracing_enabled=True,
            traces=search.list_summaries(minutes=minutes, kind=kind),
        )

    def trace(self, trace_id: str) -> TraceDetail | None:
        self._require_proxy()
        if not TRACE_ID.fullmatch(trace_id):
            raise ValueError("trace ID는 32자리 16진수여야 합니다.")
        payload = self.proxy.get(TelemetryBackend.TEMPO, "api/traces/" + trace_id)
        if payload is None:
            return None
        detail = trace_detail(payload, self.env.application, self.env.environment_id, trace_id)
        if detail:
            detail.logs = self.app_logs(
                minutes=TRACE_LOG_MINUTES,
                filter_text=trace_id,
                limit=TRACE_LOG_LIMIT,
                start=detail.started_at - TRACE_LOG_PADDING,
                end=detail.started_at
                + timedelta(milliseconds=detail.duration_ms)
                + TRACE_LOG_PADDING,
            ).lines
        return detail
