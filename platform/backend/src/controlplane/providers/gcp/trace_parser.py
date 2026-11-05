from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from controlplane.providers.telemetry import TraceDetail, TraceSpan

MAX_DEPTH = 30
MAX_WALK_DEPTH = 100

STATUS_LABELS = ("http.status_code", "http.response.status_code", "http/status_code")
PATH_LABELS = (
    "http.route",
    "url.path",
    "http.target",
    "http/path",
    "http.url",
    "url.full",
    "http/url",
)


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def label(labels, *names):
    return next((labels[k] for n in names for k in (n, "/" + n) if k in labels), None)


def path_only(value):
    return (urlsplit(str(value)).path or "/")[:500] if value else None


def own_span(span, env):
    a = span.get("labels", {})
    if label(a, "oneport.service"):
        return (
            label(a, "oneport.service") == env.service
            and label(a, "oneport.application") == env.application
            and label(a, "oneport.environment") == env.environment_id
        )
    return (
        label(a, "g.co/r/cloud_run_revision/service_name") == env.service
        and label(a, "g.co/r/cloud_run_revision/location") == env.region
    )


@dataclass
class SpanNode:
    id: str
    parent_id: str
    name: str
    kind: str
    namespace: str | None
    start: datetime
    end: datetime
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

    def to_span(self, parent_id, depth, origin) -> TraceSpan:
        return TraceSpan(
            id=self.id,
            parent_id=parent_id,
            name=self.name,
            kind=self.kind,
            namespace=self.namespace,
            depth=min(depth, MAX_DEPTH),
            start_ms=round((self.start - origin).total_seconds() * 1000, 3),
            duration_ms=round((self.end - self.start).total_seconds() * 1000, 3),
            in_progress=self.in_progress,
            error=self.error,
            fault=self.fault,
            throttle=self.throttle,
            http_method=self.http_method,
            http_path=self.http_path,
            http_status=self.http_status,
            sql=self.sql,
            exception=self.exception,
        )


def _status_code(labels):
    code = label(labels, *STATUS_LABELS)
    try:
        return int(code) if code is not None else None
    except (ValueError, TypeError):
        return None


def parse_span(span, env) -> SpanNode | None:
    if not own_span(span, env) or not span.get("spanId") or not span.get("startTime"):
        return None
    a = span.get("labels", {})
    start = timestamp(span["startTime"])
    end = max(start, timestamp(span.get("endTime", span["startTime"])))
    code = _status_code(a)
    exception = label(a, "exception.message", "error/message", "error/name")
    error = str(label(a, "oneport.error", "error")).lower() == "true" or bool(exception)
    sql = label(a, "db.statement", "db.query.text")
    return SpanNode(
        id=str(span["spanId"]),
        parent_id=str(span.get("parentSpanId", "0")),
        name=str(span.get("name", "span"))[:300],
        kind="segment" if span.get("kind") == "RPC_SERVER" else "subsegment",
        namespace=label(a, "db.system", "db.system.name"),
        start=start,
        end=end,
        in_progress="endTime" not in span,
        error=error or (code is not None and 400 <= code < 500),
        fault=(code is not None and code >= 500) or (error and code is None),
        throttle=code == 429,
        http_method=label(a, "http.method", "http.request.method", "http/method"),
        http_path=path_only(label(a, *PATH_LABELS)),
        http_status=code,
        sql=str(sql)[:500] if sql else None,
        exception=str(exception)[:300] if exception else None,
        release=label(a, "oneport.release", "service.version", "service/version"),
    )


def order_spans(nodes: dict[str, SpanNode], origin) -> list[TraceSpan]:
    ordered = sorted(nodes.values(), key=lambda n: n.start)
    children: dict[str | None, list[SpanNode]] = {}
    for n in ordered:
        parent = n.parent_id if n.parent_id in nodes else None
        children.setdefault(parent, []).append(n)
    spans, visited = [], set()

    def walk(node, parent, depth):
        if node.id in visited:
            return
        visited.add(node.id)
        spans.append(node.to_span(parent, depth, origin))
        if depth < MAX_WALK_DEPTH:
            for child in children.get(node.id, []):
                walk(child, node.id, depth + 1)

    for node in children.get(None, []):
        walk(node, None, 0)
    for node in ordered:
        walk(node, None, 0)
    return spans


def trace_detail(payload, env, trace_id):
    if payload.get("traceId") != trace_id or payload.get("projectId") != env.project_id:
        return None
    nodes: dict[str, SpanNode] = {}
    for raw in payload.get("spans", []):
        node = parse_span(raw, env)
        if node is not None:
            nodes[node.id] = node
    if not nodes:
        return None
    origin = min(n.start for n in nodes.values())
    finish = max(n.end for n in nodes.values())
    ordered = sorted(nodes.values(), key=lambda n: n.start)
    root = next((n for n in ordered if n.kind == "segment"), ordered[0])
    return TraceDetail(
        id=trace_id,
        application=env.application,
        environment_id=env.environment_id,
        started_at=origin,
        duration_ms=(finish - origin).total_seconds() * 1000,
        release=root.release or next((n.release for n in ordered if n.release), None),
        spans=order_spans(nodes, origin),
        logs=[],
    )
