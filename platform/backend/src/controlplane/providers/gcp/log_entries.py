import json
import re

from controlplane.providers.gcp.trace_parser import iso, timestamp


def build_filter(env, start, end, trace_id=None, filter_text=None) -> str:
    parts = [
        'resource.type="cloud_run_revision"',
        f"resource.labels.project_id={json.dumps(env.project_id)}",
        f"resource.labels.location={json.dumps(env.region)}",
        f"resource.labels.service_name={json.dumps(env.service)}",
        f"timestamp>={json.dumps(iso(start))}",
        f"timestamp<={json.dumps(iso(end))}",
    ]
    if trace_id:
        full = f"projects/{env.project_id}/traces/{trace_id}"
        parts.append(
            f"(trace={json.dumps(full)} OR jsonPayload.otelTraceID={json.dumps(trace_id)} OR jsonPayload.trace_id={json.dumps(trace_id)} OR textPayload:{json.dumps('trace_id=' + trace_id)})"
        )
    if filter_text:
        parts.append(
            f"(textPayload:{json.dumps(filter_text)} OR jsonPayload.message:{json.dumps(filter_text)})"
        )
    return " AND ".join(parts)


def _belongs_to_service(resource, env) -> bool:
    labels = resource.get("labels", {})
    return resource.get("type") == "cloud_run_revision" and (
        labels.get("project_id"),
        labels.get("location"),
        labels.get("service_name"),
    ) == (env.project_id, env.region, env.service)


def _entry_text(entry, payload) -> str:
    return entry.get("textPayload") or json.dumps(
        payload or entry.get("httpRequest", {}), ensure_ascii=False
    )


def _matches_trace(entry, payload, text, env, trace_id) -> bool:
    return (
        entry.get("trace") == f"projects/{env.project_id}/traces/{trace_id}"
        or payload.get("otelTraceID") == trace_id
        or payload.get("trace_id") == trace_id
        or bool(re.search(r"trace_id=" + trace_id + r"\b", text))
    )


def to_lines(entries, env, trace_id=None) -> list[dict]:
    lines = []
    for entry in entries:
        resource = entry.get("resource", {})
        if not _belongs_to_service(resource, env):
            continue
        payload = entry.get("jsonPayload", {})
        text = _entry_text(entry, payload)
        if trace_id and not _matches_trace(entry, payload, text, env, trace_id):
            continue
        lines.append(
            {
                "at": timestamp(entry["timestamp"]),
                "stream": resource.get("labels", {}).get("revision_name"),
                "message": text[:10000],
            }
        )
    lines.sort(key=lambda v: v["at"])
    return lines
