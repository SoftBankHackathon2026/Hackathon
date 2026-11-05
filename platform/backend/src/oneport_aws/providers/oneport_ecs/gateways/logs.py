from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from oneport_aws.providers.oneport_ecs.gateways.base import AwsGateway, Payload

NOT_FOUND = frozenset({"ResourceNotFoundException"})
FINISHED_QUERY = frozenset({"Complete", "Failed", "Cancelled", "Timeout"})


@dataclass(frozen=True)
class LogEvent:
    at: datetime
    stream: str | None
    message: str

    @classmethod
    def from_api(cls, raw: Payload, stream: str | None = None) -> "LogEvent":
        return cls(
            at=datetime.fromtimestamp(raw["timestamp"] / 1000, tz=UTC),
            stream=stream or raw.get("logStreamName"),
            message=raw.get("message", "").rstrip("\n"),
        )


@dataclass(frozen=True)
class LogPage:
    events: list[LogEvent]
    next_token: str | None


class LogsGateway(AwsGateway):
    def filter_events(
        self,
        *,
        group: str,
        stream_prefix: str,
        start_ms: int,
        end_ms: int | None = None,
        pattern: str | None = None,
        token: str | None = None,
    ) -> LogPage:
        kwargs: dict[str, Any] = {
            "logGroupName": group,
            "logStreamNamePrefix": stream_prefix,
            "startTime": start_ms,
        }
        if end_ms is not None:
            kwargs["endTime"] = end_ms
        if pattern:
            kwargs["filterPattern"] = pattern
        if token:
            kwargs["nextToken"] = token
        found = self._read_optional("filter_log_events", NOT_FOUND, **kwargs) or {}
        return LogPage(
            [LogEvent.from_api(e) for e in found.get("events", [])], found.get("nextToken")
        )

    def get_stream_events(self, group: str, stream: str) -> list[LogEvent]:
        found = self._read_optional(
            "get_log_events",
            NOT_FOUND,
            logGroupName=group,
            logStreamName=stream,
            startFromHead=True,
            limit=500,
        )
        return [LogEvent.from_api(e, stream) for e in (found or {}).get("events", [])]

    def start_query(self, group: str, start: int, end: int, query: str) -> str:
        return self._write(
            "start_query", logGroupName=group, startTime=start, endTime=end, queryString=query
        )["queryId"]

    def get_query_results(self, query_id: str) -> tuple[bool, list[dict[str, str]]]:
        result = self._read("get_query_results", queryId=query_id)
        rows = [{f["field"]: f["value"] for f in row} for row in result.get("results", [])]
        return result["status"] in FINISHED_QUERY, rows

    def start_live_tail(self, group_arn: str, stream_prefix: str) -> Iterator[LogEvent]:
        response = self._write(
            "start_live_tail",
            logGroupIdentifiers=[group_arn],
            logStreamNamePrefixes=[stream_prefix],
        )
        for event in response["responseStream"]:
            for item in event.get("sessionUpdate", {}).get("sessionResults", []):
                yield LogEvent.from_api(item)
