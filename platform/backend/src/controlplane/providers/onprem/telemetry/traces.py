import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from enum import StrEnum

from controlplane.providers.onprem.observability import TelemetryBackend
from controlplane.providers.onprem.telemetry.otlp import identifier, path_only, span_set_status
from controlplane.providers.telemetry import TraceSummary

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
SELECTED_ATTRIBUTES = " | select(span.http.response.status_code, span.http.status_code)"
SEARCH_LIMIT = 50


class FailureClass(StrEnum):
    ERROR = "error"
    FAULT = "fault"
    THROTTLE = "throttle"


FAILURE_FILTERS = {
    FailureClass.ERROR: "status = error || span.http.response.status_code >= 400 || span.http.status_code >= 400",
    FailureClass.FAULT: "span.http.response.status_code >= 500 || span.http.status_code >= 500",
    FailureClass.THROTTLE: "span.http.response.status_code = 429 || span.http.status_code = 429",
}
KIND_FILTERS = {
    "all": "",
    "errors": f" && ({FAILURE_FILTERS[FailureClass.ERROR]})",
    "slow": " && duration > 1s",
}


def app_scope(application: str, environment_id: str) -> str:
    return (
        f"resource.oneport.application = {json.dumps(application)} && "
        f"resource.oneport.environment = {json.dumps(environment_id)}"
    )


def _trace_ids(rows: list[dict]) -> set[str]:
    return {own_id for row in rows if (own_id := identifier(row.get("traceID"), 16)) is not None}


class TempoTraceSearch:
    def __init__(self, proxy, application: str, environment_id: str):
        self.proxy = proxy
        self.scope = app_scope(application, environment_id)

    def _search(self, window: dict, query: str, limit: int) -> list[dict]:
        result = self.proxy.get(
            TelemetryBackend.TEMPO,
            "api/search",
            {**window, "limit": limit, "q": query},
        )
        return result.get("traces", [])

    def _failures(self, window: dict, ids: set[str]) -> dict[FailureClass, dict[str, int | None]]:
        failures: dict[FailureClass, dict[str, int | None]] = {name: {} for name in FailureClass}
        if not ids:
            return failures
        trace_ids = " || ".join(f'trace:id = "{own_id}"' for own_id in sorted(ids))

        def search_failure(name: FailureClass):
            query = (
                f"{{{self.scope} && ({trace_ids}) && ({FAILURE_FILTERS[name]})}}"
                f"{SELECTED_ATTRIBUTES}"
            )
            rows = self._search(window, query, len(ids))
            return name, {
                own_id: span_set_status(row)
                for row in rows
                if (own_id := identifier(row.get("traceID"), 16)) in ids
            }

        with ThreadPoolExecutor(max_workers=3) as pool:
            failures.update(pool.map(search_failure, FAILURE_FILTERS))
        return failures

    def list_summaries(self, *, minutes: int, kind: str) -> list[TraceSummary]:
        end = int(datetime.now(UTC).timestamp())
        window = {"start": end - minutes * 60, "end": end, "spss": 1}
        query = "{" + self.scope + KIND_FILTERS[kind] + "}" + SELECTED_ATTRIBUTES
        rows = self._search(window, query + " with (most_recent=true)", SEARCH_LIMIT)
        failures = self._failures(window, _trace_ids(rows))
        summaries = [
            summary for row in rows if (summary := self._summary(row, failures, kind)) is not None
        ]
        summaries.sort(key=lambda s: s.started_at, reverse=True)
        return summaries

    @staticmethod
    def _summary(row: dict, failures: dict, kind: str) -> TraceSummary | None:
        own_id = identifier(row.get("traceID"), 16)
        if own_id is None:
            return None
        name = row.get("rootTraceName", "")
        method, _, path = name.partition(" ")
        is_http = method in HTTP_METHODS
        codes = [span_set_status(row), *[found.get(own_id) for found in failures.values()]]
        return TraceSummary(
            id=own_id,
            started_at=datetime.fromtimestamp(int(row["startTimeUnixNano"]) / 1e9, UTC),
            duration_ms=row.get("durationMs", 0),
            method=method if is_http else None,
            path=path_only(path) if is_http else name[:300],
            status=max((code for code in codes if code is not None), default=None),
            error=kind == "errors" or own_id in failures[FailureClass.ERROR],
            fault=own_id in failures[FailureClass.FAULT],
            throttle=own_id in failures[FailureClass.THROTTLE],
        )
