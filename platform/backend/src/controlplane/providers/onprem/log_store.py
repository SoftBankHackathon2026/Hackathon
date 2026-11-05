"""Read retained app logs without requiring their original Pods to still exist."""

import json
from datetime import UTC, datetime, timedelta

from controlplane.providers.contracts import ProviderUnavailable
from controlplane.providers.interface import AppLogLine, AppLogs
from controlplane.providers.onprem.observability import TelemetryBackend

RETENTION_MINUTES = 7 * 24 * 60
POD_LOG_LOOKBACK_MINUTES = 360
NANOS_PER_SECOND = 1_000_000_000
MAX_MESSAGE_CHARS = 10000
QUERY_PATH = "loki/api/v1/query_range"


def uses_loki(settings) -> bool:
    config = getattr(settings, "observability", None)
    return getattr(config, "logs_backend", None) == "loki"


def max_lookback(settings) -> int:
    return RETENTION_MINUTES if uses_loki(settings) else POD_LOG_LOOKBACK_MINUTES


def stream_labels(env) -> dict[str, str]:
    return {
        "namespace": env.namespace,
        "application": env.application,
        "environment": env.environment_id,
        "container": "app",
    }


def build_query(labels: dict[str, str], filter_text: str | None) -> str:
    query = "{" + ",".join(f"{k}={json.dumps(v)}" for k, v in labels.items()) + "}"
    if filter_text:
        query += " |= " + json.dumps(filter_text, ensure_ascii=False)
    return query


def _timestamp(nanoseconds: str) -> datetime:
    seconds, nanos = divmod(int(nanoseconds), NANOS_PER_SECOND)
    return datetime.fromtimestamp(seconds, UTC) + timedelta(microseconds=nanos // 1000)


def parse_streams(
    payload: dict, labels: dict[str, str], start: datetime, end: datetime, filter_text: str | None
) -> list[AppLogLine]:
    lines = []
    for result in payload["data"]["result"]:
        stream = result.get("stream", {})
        if any(stream.get(key) != value for key, value in labels.items()):
            continue
        for nanoseconds, message, *_ in result.get("values", []):
            at = _timestamp(nanoseconds)
            if not start <= at <= end or (filter_text and filter_text not in message):
                continue
            lines.append(
                AppLogLine(
                    at=at, stream=stream.get("pod", "app"), message=message[:MAX_MESSAGE_CHARS]
                )
            )
    return lines


def stored_logs(proxy, env, *, minutes, filter_text, limit, start=None, end=None) -> AppLogs:
    end = end or datetime.now(UTC)
    start = start or end - timedelta(minutes=minutes)
    labels = stream_labels(env)
    payload = proxy.get(
        TelemetryBackend.LOKI,
        QUERY_PATH,
        {
            "query": build_query(labels, filter_text),
            "start": str(int(start.timestamp() * NANOS_PER_SECOND)),
            "end": str(int(end.timestamp() * NANOS_PER_SECOND)),
            "direction": "backward",
            "limit": limit + 1,
        },
    )
    if payload.get("status") != "success" or payload.get("data", {}).get("resultType") != "streams":
        raise ProviderUnavailable("보관된 앱 로그를 조회하지 못했습니다.")
    lines = parse_streams(payload, labels, start, end, filter_text)
    lines.sort(key=lambda line: (line.at, line.stream, line.message))
    return AppLogs(
        application=env.application,
        environment_id=env.environment_id,
        minutes=minutes,
        max_lookback_minutes=RETENTION_MINUTES,
        lines=lines[-limit:],
        truncated=len(lines) > limit,
    )
