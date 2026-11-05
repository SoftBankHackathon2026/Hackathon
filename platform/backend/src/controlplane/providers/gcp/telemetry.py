"""Bounded, app-scoped Cloud Trace, Logging and Monitoring reads.

The platform stores no telemetry. Detail reads recheck every span's app identity;
knowing a project-wide trace ID never grants access to another app's spans/logs.
"""

import re
from datetime import UTC, datetime, timedelta

import requests
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession

from controlplane.execution.gcp.auth import credentials
from controlplane.providers.contracts import ProviderUnavailable
from controlplane.providers.gcp import log_entries, traffic_series
from controlplane.providers.gcp.trace_parser import iso, trace_detail
from controlplane.providers.interface import AppLogLine, AppLogs
from controlplane.providers.telemetry import (
    TraceList,
    TraceSummary,
    Traffic,
    latest_resource_sample,
)

__all__ = ["GcpTelemetry", "trace_detail"]

TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_PAGES = 5
MAX_TRACES = 50
SLOW_TRACE_MS = 1000
LOGGING_API = "https://logging.googleapis.com/v2/entries:list"
TRACE_API = "https://cloudtrace.googleapis.com/v1/projects/"
MONITORING_API = "https://monitoring.googleapis.com/v3/projects/"


class GcpTelemetry:
    def __init__(self, settings, session=None, now=None):
        self.settings, self.env = settings, settings.environment
        self.session = session
        self._owned = session is None
        self.now = now or (lambda: datetime.now(UTC))

    def __enter__(self):
        if self.session is None:
            try:
                self.session = AuthorizedSession(credentials(self.settings))
            except GoogleAuthError as exc:
                raise ProviderUnavailable("GCP monitoring authentication failed") from exc
        return self

    def __exit__(self, *_):
        if self._owned:
            self.session.close()

    def call(self, method, url, *, missing=False, **kwargs):
        try:
            response = self.session.request(method, url, timeout=15, **kwargs)
            if missing and response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, GoogleAuthError, ValueError) as exc:
            raise ProviderUnavailable("GCP monitoring read failed") from exc

    def log_entries(self, start, end, *, trace_id=None, filter_text=None, limit=200):
        query = log_entries.build_filter(self.env, start, end, trace_id, filter_text)
        entries, token = [], None
        for _ in range(MAX_PAGES):
            body = {
                "resourceNames": [f"projects/{self.env.project_id}"],
                "filter": query,
                "orderBy": "timestamp desc",
                "pageSize": min(limit, 1000),
            }
            if token:
                body["pageToken"] = token
            data = self.call("POST", LOGGING_API, json=body)
            entries.extend(data.get("entries", []))
            token = data.get("nextPageToken")
            if len(entries) >= limit or not token:
                break
        lines = log_entries.to_lines(entries[:limit], self.env, trace_id)
        return lines, bool(token) or len(entries) > limit

    def app_logs(self, *, minutes, filter_text, limit):
        end = self.now()
        lines, truncated = self.log_entries(
            end - timedelta(minutes=minutes), end, filter_text=filter_text, limit=limit
        )
        return AppLogs(
            application=self.env.application,
            environment_id=self.env.environment_id,
            minutes=minutes,
            lines=lines,
            truncated=truncated,
        )

    def traces(self, *, minutes, kind):
        end = self.now()
        rows: dict[str, TraceSummary] = {}
        for scope in self._trace_scopes():
            self._collect_traces(rows, scope, end, minutes, kind)
        return TraceList(
            application=self.env.application,
            environment_id=self.env.environment_id,
            minutes=minutes,
            kind=kind,
            tracing_enabled=True,
            traces=sorted(rows.values(), key=lambda r: r.started_at, reverse=True)[:MAX_TRACES],
        )

    def _trace_scopes(self):
        env = self.env
        scope = f"+oneport.service:{env.service} +oneport.environment:{env.environment_id}"
        return [
            scope + " label:/http/method",
            scope + " label:http.request.method",
            f"+g.co/r/cloud_run_revision/service_name:{env.service} +g.co/r/cloud_run_revision/location:{env.region}",
        ]

    def _collect_traces(self, rows, scope, end, minutes, kind):
        params = {
            "startTime": iso(end - timedelta(minutes=minutes)),
            "endTime": iso(end),
            "view": "COMPLETE",
            "orderBy": "start desc",
            "pageSize": 100,
            "filter": scope,
        }
        for _ in range(MAX_PAGES):
            data = self.call("GET", f"{TRACE_API}{self.env.project_id}/traces", params=params)
            for raw in data.get("traces", []):
                summary = self._summarize(raw, kind)
                if summary is not None:
                    rows[summary.id] = summary
            token = data.get("nextPageToken")
            if not token or len(rows) >= MAX_TRACES:
                break
            params["pageToken"] = token

    def _summarize(self, raw, kind) -> TraceSummary | None:
        tid = raw.get("traceId", "")
        if not TRACE_ID.fullmatch(tid):
            return None
        detail = trace_detail(raw, self.env, tid)
        if not detail:
            return None
        spans = detail.spans
        root = next((s for s in spans if s.kind == "segment" and s.http_method), None)
        if root is None:
            return None
        flags = {k: any(getattr(s, k) for s in spans) for k in ("error", "fault", "throttle")}
        if kind == "errors" and not (flags["error"] or flags["fault"]):
            return None
        if kind == "slow" and detail.duration_ms < SLOW_TRACE_MS:
            return None
        return TraceSummary(
            id=tid,
            started_at=detail.started_at,
            duration_ms=detail.duration_ms,
            method=root.http_method,
            path=root.http_path,
            status=root.http_status,
            **flags,
        )

    def trace(self, trace_id):
        if not TRACE_ID.fullmatch(trace_id):
            raise ValueError("trace ID 형식이 아닙니다.")
        raw = self.call("GET", f"{TRACE_API}{self.env.project_id}/traces/{trace_id}", missing=True)
        detail = trace_detail(raw, self.env, trace_id) if raw else None
        if detail:
            lines, _ = self.log_entries(
                detail.started_at - timedelta(minutes=1),
                detail.started_at + timedelta(milliseconds=detail.duration_ms, minutes=5),
                trace_id=trace_id,
            )
            detail.logs = [AppLogLine.model_validate(line) for line in lines]
        return detail

    def metric(self, name, start, end, period, *, aligner, reducer, groups):
        env = self.env
        params = {
            "filter": f'metric.type="run.googleapis.com/{name}" AND resource.type="cloud_run_revision" AND resource.labels.service_name="{env.service}" AND resource.labels.location="{env.region}"',
            "interval.startTime": iso(start),
            "interval.endTime": iso(end),
            "aggregation.alignmentPeriod": f"{period}s",
            "aggregation.perSeriesAligner": aligner,
            "aggregation.crossSeriesReducer": reducer,
            "aggregation.groupByFields": groups,
            "pageSize": 1000,
        }
        result = []
        for _ in range(MAX_PAGES):
            data = self.call("GET", f"{MONITORING_API}{env.project_id}/timeSeries", params=params)
            result.extend(data.get("timeSeries", []))
            if not data.get("nextPageToken"):
                return result
            params["pageToken"] = data["nextPageToken"]
        return result

    def _resources(self, now):
        result = {"scope": "service", "sample_window_seconds": 60}
        for key in ("cpu", "memory"):
            try:
                series = self.metric(
                    f"container/{key}/utilizations",
                    now - timedelta(minutes=10),
                    now,
                    60,
                    aligner="ALIGN_DELTA",
                    reducer="REDUCE_NONE",
                    groups=[],
                )
            except ProviderUnavailable:
                continue
            value, sampled_at = traffic_series.latest_utilization(series, self.env, now)
            result[f"{key}_percent"] = value
            result[f"{key}_sampled_at"] = sampled_at
        if any(result.get(f"{key}_percent") is not None for key in ("cpu", "memory")):
            return result
        return None

    def _running(self, now):
        end = traffic_series.floor_time(now, 60)
        try:
            instances = self.metric(
                "container/instance_count",
                end - timedelta(minutes=10),
                end,
                60,
                aligner="ALIGN_MAX",
                reducer="REDUCE_SUM",
                groups=["resource.labels.revision_name"],
            )
        except ProviderUnavailable:
            return None, None
        return latest_resource_sample(traffic_series.instance_counts(instances), now)

    def _traffic_metrics(self, start, end, period):
        groups = ["resource.labels.revision_name"]
        counts = self.metric(
            "request_count",
            start,
            end,
            period,
            aligner="ALIGN_SUM",
            reducer="REDUCE_SUM",
            groups=groups + ["metric.labels.response_code_class"],
        )
        latency = self.metric(
            "request_latencies",
            start,
            end,
            period,
            aligner="ALIGN_DELTA",
            reducer="REDUCE_PERCENTILE_95",
            groups=groups,
        )
        instances = self.metric(
            "container/instance_count",
            start,
            end,
            period,
            aligner="ALIGN_MAX",
            reducer="REDUCE_SUM",
            groups=groups,
        )
        return (("requests", counts), ("p95_ms", latency), ("healthy", instances))

    def traffic(self, *, minutes):
        now = self.now()
        period = 60 if minutes <= 180 else 300
        end = traffic_series.floor_time(now, period)
        start = end - timedelta(minutes=minutes)
        metrics = self._traffic_metrics(start, end, period)
        service = self.call("GET", "https://run.googleapis.com/v2/" + self.env.service_name)
        if service.get("uid") != self.env.service_uid:
            raise ProviderUnavailable("Registered Cloud Run service UID changed")
        weights = traffic_series.service_weights(service)
        revisions, points = traffic_series.collect_points(metrics, period)
        grid = [start + timedelta(seconds=period * i) for i in range(minutes * 60 // period)]
        series = traffic_series.build_series(revisions | set(weights), weights, points, grid)
        live = next((r for r, w in weights.items() if w == 100), None)
        running, running_at = self._running(now)
        return Traffic(
            application=self.env.application,
            environment_id=self.env.environment_id,
            minutes=minutes,
            period_seconds=period,
            source="cloud_monitoring",
            series=series,
            live_release=live,
            deployment_status="IN_PROGRESS" if service.get("reconciling") else None,
            resource_usage=self._resources(now),
            running=int(running) if running is not None else None,
            running_sampled_at=running_at,
        )
