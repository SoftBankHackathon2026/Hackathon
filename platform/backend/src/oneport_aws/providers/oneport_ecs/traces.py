"""X-Ray trace summaries and one trace as a span tree plus the app log lines that
carry its trace ID. X-Ray is account-wide, so every query is narrowed to this
app's service name."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from controlplane.providers.interface import AppLogLine
from controlplane.providers.telemetry import TraceDetail, TraceKind, TraceSpan, TraceSummary
from oneport_aws.providers.oneport_ecs import logs
from oneport_aws.providers.oneport_ecs.gateways.base import Payload
from oneport_aws.providers.oneport_ecs.provider import OneportEcsProvider

TRACE_ID = re.compile(r"^1-[0-9a-f]{8}-[0-9a-f]{24}$")
KINDS: dict[str, str] = {
    "all": "",
    "errors": " AND (error OR fault)",
    "slow": " AND responsetime > 1",
}
SUMMARY_PAGES = 5
SQL_LIMIT = 500
EXCEPTION_LIMIT = 300


@dataclass(frozen=True)
class _Node:
    id: str
    parent_id: str | None
    name: str
    kind: str
    namespace: str | None
    start: float
    end: float
    in_progress: bool
    error: bool
    fault: bool
    throttle: bool
    http_method: str | None
    http_path: str | None
    http_status: int | None
    sql: str | None
    exception: str | None
    release: str | None


def _path(url: str | None) -> str | None:
    return (urlsplit(url).path or "/") if url else None


def _started(summary: Payload) -> datetime:
    if summary.get("StartTime"):
        return summary["StartTime"].astimezone(UTC)
    return datetime.fromtimestamp(int(summary["Id"][2:10], 16), UTC)


def _summary(raw: Payload) -> TraceSummary:
    http = raw.get("Http", {})
    return TraceSummary(
        id=raw["Id"],
        started_at=_started(raw),
        duration_ms=round((raw.get("Duration") or 0) * 1000, 1),
        method=http.get("HttpMethod"),
        path=_path(http.get("HttpURL")),
        status=http.get("HttpStatus"),
        error=bool(raw.get("HasError")),
        fault=bool(raw.get("HasFault")),
        throttle=bool(raw.get("HasThrottle")),
    )


def traces(
    provider: OneportEcsProvider, *, minutes: int, kind: TraceKind, limit: int = 50
) -> list[TraceSummary]:
    env = provider.environment
    end = datetime.now(UTC)
    found = provider.gateways.xray.list_trace_summaries(
        start=end - timedelta(minutes=minutes),
        end=end,
        filter_expression=f'service("{env.application}")' + KINDS[kind],
        limit=limit,
        max_pages=SUMMARY_PAGES,
    )
    rows = sorted((_summary(raw) for raw in found), key=lambda row: row.started_at, reverse=True)
    return rows[:limit]


def _exception(doc: Payload) -> str | None:
    for item in doc.get("cause", {}).get("exceptions", []) or []:
        text = item.get("message") or item.get("type")
        if text:
            return text[:EXCEPTION_LIMIT]
    return None


def _node(doc: Payload, parent: str | None, kind: str) -> _Node:
    start = doc.get("start_time", 0.0)
    request = doc.get("http", {}).get("request", {})
    return _Node(
        id=doc["id"],
        parent_id=parent,
        name=doc.get("name", ""),
        kind=kind,
        namespace=doc.get("namespace"),
        start=start,
        end=doc.get("end_time") or start,
        in_progress=bool(doc.get("in_progress")),
        error=bool(doc.get("error")),
        fault=bool(doc.get("fault")),
        throttle=bool(doc.get("throttle")),
        http_method=request.get("method"),
        http_path=_path(request.get("url")),
        http_status=doc.get("http", {}).get("response", {}).get("status"),
        sql=doc.get("sql", {}).get("sanitized_query") or None,
        exception=_exception(doc),
        release=doc.get("service", {}).get("version"),
    )


def _collect(doc: Payload, parent: str | None, kind: str, nodes: dict[str, _Node]) -> None:
    nodes[doc["id"]] = _node(doc, parent, kind)
    for child in doc.get("subsegments", []) or []:
        _collect(child, doc["id"], "subsegment", nodes)


def _span(node: _Node, parent: str | None, depth: int, origin: float) -> TraceSpan:
    return TraceSpan(
        id=node.id,
        parent_id=parent,
        name=node.name,
        kind=node.kind,
        namespace=node.namespace,
        depth=depth,
        start_ms=round((node.start - origin) * 1000, 2),
        duration_ms=round((node.end - node.start) * 1000, 2),
        in_progress=node.in_progress,
        error=node.error,
        fault=node.fault,
        throttle=node.throttle,
        http_method=node.http_method,
        http_path=node.http_path,
        http_status=node.http_status,
        sql=node.sql[:SQL_LIMIT] if node.sql else None,
        exception=node.exception,
    )


def _span_tree(nodes: dict[str, _Node], origin: float) -> list[TraceSpan]:
    children: dict[str | None, list[_Node]] = {}
    for node in nodes.values():
        parent = node.parent_id if node.parent_id in nodes else None
        children.setdefault(parent, []).append(node)
    spans: list[TraceSpan] = []

    def walk(parent: str | None, depth: int) -> None:
        for node in sorted(children.get(parent, []), key=lambda n: n.start):
            spans.append(_span(node, parent, depth, origin))
            walk(node.id, depth + 1)

    walk(None, 0)
    return spans


def _trace_logs(
    provider: OneportEcsProvider, trace_id: str, started: datetime, finished: datetime
) -> list[AppLogLine]:
    hex_id = trace_id[2:10] + trace_id[11:]
    lines, _ = logs.latest(
        provider,
        filter_text=hex_id,
        start=started - timedelta(minutes=1),
        end=finished + timedelta(minutes=5),
        limit=200,
    )
    return lines


def trace(provider: OneportEcsProvider, trace_id: str) -> TraceDetail | None:
    if not TRACE_ID.fullmatch(trace_id):
        raise ValueError("trace ID 형식이 아닙니다.")
    env = provider.environment
    docs = provider.gateways.xray.get_trace_documents(trace_id)
    if not docs or not any(doc.get("name") == env.application for doc in docs):
        return None
    nodes: dict[str, _Node] = {}
    for doc in docs:
        _collect(doc, doc.get("parent_id"), "segment", nodes)
    origin = min(node.start for node in nodes.values())
    finish = max(node.end for node in nodes.values())
    root = next(
        (n for n in nodes.values() if n.name == env.application and n.kind == "segment"), None
    )
    started = datetime.fromtimestamp(origin, UTC)
    return TraceDetail(
        id=trace_id,
        application=env.application,
        environment_id=env.environment_id,
        started_at=started,
        duration_ms=round((finish - origin) * 1000, 2),
        release=root.release if root else None,
        spans=_span_tree(nodes, origin),
        logs=_trace_logs(provider, trace_id, started, datetime.fromtimestamp(finish, UTC)),
    )
