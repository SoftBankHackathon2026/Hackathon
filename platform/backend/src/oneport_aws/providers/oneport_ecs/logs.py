"""App logs (infra Notion §4). All apps share /ecs/oneport/apps; stream names
are `<app-id>/<container>/<task-id>`, so the app id prefix selects one app."""

import time
from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from controlplane.providers.interface import AppLogLine
from oneport_aws.providers.oneport_ecs.gateways.logs import LogEvent
from oneport_aws.providers.oneport_ecs.provider import OneportEcsProvider


def _line(event: LogEvent) -> AppLogLine:
    return AppLogLine(at=event.at, stream=event.stream, message=event.message)


def _epoch_ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def _filter_pattern(filter_text: str | None) -> str | None:
    if not filter_text:
        return None
    if '"' in filter_text:
        raise ValueError('검색어에 " 는 쓸 수 없습니다.')
    return f'"{filter_text}"'


def latest(
    provider: OneportEcsProvider,
    *,
    minutes: int = 30,
    container: str | None = None,
    filter_text: str | None = None,
    limit: int = 200,
    max_pages: int = 5,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[list[AppLogLine], bool]:
    env = provider.environment
    prefix = f"{env.application}/{container}/" if container else f"{env.application}/"
    start = start or datetime.now(UTC) - timedelta(minutes=minutes)
    pattern = _filter_pattern(filter_text)
    lines: deque[AppLogLine] = deque(maxlen=limit)
    token: str | None = None
    for _ in range(max_pages):
        page = provider.gateways.logs.filter_events(
            group=env.log_group,
            stream_prefix=prefix,
            start_ms=_epoch_ms(start),
            end_ms=_epoch_ms(end) if end else None,
            pattern=pattern,
            token=token,
        )
        lines.extend(_line(event) for event in page.events)
        if not page.next_token:
            return list(lines), False
        token = page.next_token
    return list(lines), True


def recent(
    provider: OneportEcsProvider,
    *,
    minutes: int = 30,
    container: str | None = None,
    limit: int = 200,
) -> list[AppLogLine]:
    return latest(provider, minutes=minutes, container=container, limit=limit)[0]


def task_log(
    provider: OneportEcsProvider, task_id: str, *, container: str | None = None
) -> list[AppLogLine]:
    env = provider.environment
    stream = f"{env.application}/{container or env.container}/{task_id}"
    events = provider.gateways.logs.get_stream_events(env.log_group, stream)
    return [_line(event) for event in events]


def query(
    provider: OneportEcsProvider,
    *,
    filter_text: str | None = None,
    minutes: int = 60,
    limit: int = 100,
    timeout: float = 30,
    poll: float = 1,
) -> list[dict[str, str]]:
    env, logs = provider.environment, provider.gateways.logs
    q = (
        "fields @timestamp, @message, @logStream"
        ' | parse @logStream "*/*/*" as app, container, task'
        f' | filter app = "{env.application}"'
    )
    if filter_text:
        if '"' in filter_text or "|" in filter_text:
            raise ValueError('검색어에 " 또는 | 는 쓸 수 없습니다.')
        q += f' | filter @message like "{filter_text}"'
    q += f" | sort @timestamp desc | limit {int(limit)}"
    now = datetime.now(UTC)
    query_id = logs.start_query(
        env.log_group,
        int((now - timedelta(minutes=minutes)).timestamp()),
        int(now.timestamp()),
        q,
    )
    deadline = time.monotonic() + timeout
    while True:
        finished, rows = logs.get_query_results(query_id)
        if finished:
            return rows
        if time.monotonic() >= deadline:
            raise TimeoutError("Logs Insights 쿼리가 끝나지 않았습니다.")
        time.sleep(poll)


def tail(provider: OneportEcsProvider, *, seconds: int = 60) -> Iterator[AppLogLine]:
    env = provider.environment
    group_arn = f"arn:aws:logs:{env.region}:{env.account_id}:log-group:{env.log_group}"
    events = provider.gateways.logs.start_live_tail(group_arn, f"{env.application}/")
    deadline = time.monotonic() + seconds
    for event in events:
        yield _line(event)
        if time.monotonic() >= deadline:
            break
