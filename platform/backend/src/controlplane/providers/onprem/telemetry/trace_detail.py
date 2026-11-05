from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from controlplane.providers.onprem.telemetry.otlp import (
    attributes,
    http_status,
    identifier,
    path_only,
)
from controlplane.providers.telemetry import TraceDetail, TraceSpan

SERVER_KINDS = (2, "SPAN_KIND_SERVER")
ERROR_STATUS_CODES = (2, "STATUS_CODE_ERROR")
MAX_REPORTED_DEPTH = 30
MAX_WALK_DEPTH = 100
NANOS_PER_MILLI = 1_000_000


@dataclass
class FoundSpan:
    id: str
    parent_id: str | None
    name: str
    kind: str
    namespace: str | None
    start: int
    end: int
    error: bool
    fault: bool
    throttle: bool
    http_method: str | None
    http_path: str | None
    http_status: int | None
    exception: str | None
    release: str | None

    def to_trace_span(self, parent_id: str | None, depth: int, origin: int) -> TraceSpan:
        return TraceSpan(
            id=self.id,
            parent_id=parent_id,
            name=self.name,
            kind=self.kind,
            namespace=self.namespace,
            depth=min(depth, MAX_REPORTED_DEPTH),
            start_ms=round((self.start - origin) / NANOS_PER_MILLI, 3),
            duration_ms=round((self.end - self.start) / NANOS_PER_MILLI, 3),
            error=self.error,
            fault=self.fault,
            throttle=self.throttle,
            http_method=self.http_method,
            http_path=self.http_path,
            http_status=self.http_status,
            sql=None,
            exception=self.exception,
        )


def _exception_text(raw: dict) -> str | None:
    exception = None
    for event in raw.get("events", []):
        if event.get("name") == "exception":
            event_tags = attributes(event.get("attributes"))
            exception = str(
                event_tags.get("exception.message", event_tags.get("exception.type", ""))
            )
    return exception[:300] if exception else None


def _found_span(raw: dict, own_id: str, resource_tags: dict) -> FoundSpan:
    tags = attributes(raw.get("attributes"))
    start, end = int(raw["startTimeUnixNano"]), int(raw["endTimeUnixNano"])
    code = http_status(tags)
    failed = raw.get("status", {}).get("code") in ERROR_STATUS_CODES
    return FoundSpan(
        id=own_id,
        parent_id=identifier(raw.get("parentSpanId", raw.get("parentSpanID"))),
        name=str(raw.get("name", "span"))[:300],
        kind="segment" if raw.get("kind") in SERVER_KINDS else "subsegment",
        namespace=tags.get("db.system.name", tags.get("db.system")),
        start=start,
        end=max(start, end),
        error=failed or (code is not None and code >= 400),
        fault=code is not None and code >= 500,
        throttle=code == 429,
        http_method=tags.get("http.request.method", tags.get("http.method")),
        http_path=path_only(tags.get("http.route", tags.get("url.path", tags.get("http.target")))),
        http_status=code,
        exception=_exception_text(raw),
        release=resource_tags.get("oneport.release"),
    )


def _app_spans(payload: dict, application: str, environment_id: str) -> Iterator[tuple[dict, dict]]:
    for resource in payload.get("resourceSpans", payload.get("batches", [])):
        tags = attributes(resource.get("resource", {}).get("attributes"))
        if (tags.get("oneport.application"), tags.get("oneport.environment")) != (
            application,
            environment_id,
        ):
            continue
        scopes = resource.get("scopeSpans", resource.get("instrumentationLibrarySpans", []))
        for scope in scopes:
            for raw in scope.get("spans", []):
                yield tags, raw


def collect_spans(
    payload: dict, application: str, environment_id: str, trace_id: str
) -> dict[str, FoundSpan]:
    found = {}
    for resource_tags, raw in _app_spans(payload, application, environment_id):
        own_trace = identifier(raw.get("traceId", raw.get("traceID")), 16)
        own_id = identifier(raw.get("spanId", raw.get("spanID")))
        if own_trace != trace_id or own_id is None:
            continue
        found[own_id] = _found_span(raw, own_id, resource_tags)
    return found


def order_spans(found: dict[str, FoundSpan]) -> list[tuple[FoundSpan, str | None, int]]:
    children: dict[str | None, list[FoundSpan]] = {}
    for span in found.values():
        parent = span.parent_id if span.parent_id in found else None
        children.setdefault(parent, []).append(span)
    ordered: list[tuple[FoundSpan, str | None, int]] = []
    visited: set[str] = set()

    def walk(parent: str | None, depth: int) -> None:
        for span in sorted(children.get(parent, []), key=lambda s: s.start):
            if span.id in visited:
                continue
            visited.add(span.id)
            ordered.append((span, parent, depth))
            if depth < MAX_WALK_DEPTH:
                walk(span.id, depth + 1)

    walk(None, 0)
    children.setdefault(None, []).extend(found[i] for i in found if i not in visited)
    walk(None, 0)
    return ordered


def trace_detail(
    payload: dict, application: str, environment_id: str, trace_id: str
) -> TraceDetail | None:
    found = collect_spans(payload, application, environment_id, trace_id)
    if not found:
        return None
    origin = min(s.start for s in found.values())
    finish = max(s.end for s in found.values())
    spans = [
        span.to_trace_span(parent, depth, origin) for span, parent, depth in order_spans(found)
    ]
    segments = [s for s in found.values() if s.kind == "segment"]
    root = segments[0] if segments else next(iter(found.values()))
    return TraceDetail(
        id=trace_id,
        application=application,
        environment_id=environment_id,
        started_at=datetime.fromtimestamp(origin / 1_000_000_000, UTC),
        duration_ms=round((finish - origin) / NANOS_PER_MILLI, 3),
        release=root.release,
        spans=spans,
        logs=[],
    )
