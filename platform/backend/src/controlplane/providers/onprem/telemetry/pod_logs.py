from datetime import UTC, datetime, timedelta

from kubernetes.client.exceptions import ApiException

from controlplane.providers.contracts import ProviderUnavailable
from controlplane.providers.interface import AppLogLine, AppLogs

MAX_PODS = 10
TAIL_LINES = 1000
LOG_BYTES = 128 * 1024
LOG_TIMEOUT = (3, 5)
APP_CONTAINER = "app"
ABSENT_POD_STATUSES = {400, 404}


def _read_pod_log(clients, env, pod, start: datetime) -> str | None:
    try:
        return clients.core.read_namespaced_pod_log(
            pod.metadata.name,
            env.namespace,
            container=APP_CONTAINER,
            timestamps=True,
            since_seconds=max(1, int((datetime.now(UTC) - start).total_seconds())),
            tail_lines=TAIL_LINES,
            limit_bytes=LOG_BYTES,
            _request_timeout=LOG_TIMEOUT,
        )
    except ApiException as exc:
        if exc.status in ABSENT_POD_STATUSES:
            return None
        raise ProviderUnavailable("앱 로그를 조회하지 못했습니다.") from None


def _parse_lines(
    raw: str, pod_name: str, start: datetime, end: datetime, filter_text: str | None
) -> list[AppLogLine]:
    lines = []
    for line in raw.splitlines():
        timestamp, _, message = line.partition(" ")
        try:
            at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if not start <= at <= end or (filter_text and filter_text not in message):
            continue
        lines.append(AppLogLine(at=at, stream=pod_name, message=message))
    return lines


def _was_truncated(raw: str) -> bool:
    return len(raw.encode()) >= LOG_BYTES or len(raw.splitlines()) >= TAIL_LINES


def live_pod_logs(
    clients,
    env,
    pods: list,
    *,
    minutes: int,
    filter_text: str | None,
    limit: int,
    start: datetime | None,
    end: datetime | None,
) -> AppLogs:
    start = start or datetime.now(UTC) - timedelta(minutes=minutes)
    end = end or datetime.now(UTC)
    lines: list[AppLogLine] = []
    truncated = len(pods) > MAX_PODS
    for pod in sorted(pods, key=lambda p: p.metadata.name)[:MAX_PODS]:
        for container in pod.spec.containers:
            if container.name != APP_CONTAINER:
                continue
            raw = _read_pod_log(clients, env, pod, start)
            if raw is None:
                continue
            truncated |= _was_truncated(raw)
            lines.extend(_parse_lines(raw, pod.metadata.name, start, end, filter_text))
    lines.sort(key=lambda line: line.at)
    return AppLogs(
        application=env.application,
        environment_id=env.environment_id,
        minutes=minutes,
        lines=lines[-limit:],
        truncated=truncated or len(lines) > limit,
    )
