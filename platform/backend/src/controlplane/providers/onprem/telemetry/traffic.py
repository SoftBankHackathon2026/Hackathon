import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime

from controlplane.providers.contracts import ProviderUnavailable
from controlplane.providers.onprem.observability import TelemetryBackend
from controlplane.providers.onprem.telemetry.otlp import number
from controlplane.providers.telemetry import TrafficPoint, TrafficSeries

SHORT_PERIOD_SECONDS = 60
LONG_PERIOD_SECONDS = 300
SHORT_PERIOD_MAX_MINUTES = 180
REQUESTS_COUNTER = "traefik_service_requests_total"
DURATION_BUCKET = "traefik_service_request_duration_seconds_bucket"
RANGE_PATH = "api/v1/query_range"
INSTANT_PATH = "api/v1/query"
TRAEFIK_UP = 'up{job="traefik"}'


@dataclass(frozen=True)
class TrafficGroup:
    color: str
    label: str
    service: str
    weight: int


@dataclass
class PointTotals:
    at: datetime
    requests: float = 0
    server_errors: float = 0
    client_errors: float = 0
    p95_ms: float | None = None

    def add_count(self, value: float, status: str) -> None:
        self.requests += max(0, value)
        if status.startswith("5"):
            self.server_errors += max(0, value)
        if status.startswith("4"):
            self.client_errors += max(0, value)

    def to_point(self) -> TrafficPoint:
        return TrafficPoint(
            at=self.at,
            requests=self.requests,
            server_errors=self.server_errors,
            client_errors=self.client_errors,
            p95_ms=self.p95_ms,
        )


@dataclass
class TrafficWindow:
    minutes: int
    period: int = field(init=False)
    end: int = field(init=False)
    grid: list[int] = field(init=False)

    def __post_init__(self) -> None:
        self.period = (
            SHORT_PERIOD_SECONDS
            if self.minutes <= SHORT_PERIOD_MAX_MINUTES
            else LONG_PERIOD_SECONDS
        )
        self.end = int(datetime.now(UTC).timestamp()) // self.period * self.period
        self.grid = list(
            range(self.end - self.minutes * 60 + self.period, self.end + 1, self.period)
        )


def traffic_groups(rollout: dict) -> list[TrafficGroup]:
    strategy = rollout["spec"]["strategy"]
    if "blueGreen" in strategy:
        blue_green = strategy["blueGreen"]
        return [
            TrafficGroup("blue", "운영", blue_green["activeService"], 100),
            TrafficGroup("green", "후보", blue_green["previewService"], 0),
        ]
    canary = strategy["canary"]
    weights = rollout.get("status", {}).get("canary", {}).get("weights", {})
    return [
        TrafficGroup(
            "blue", "안정", canary["stableService"], weights.get("stable", {}).get("weight", 100)
        ),
        TrafficGroup(
            "green", "카나리", canary["canaryService"], weights.get("canary", {}).get("weight", 0)
        ),
    ]


def service_patterns(namespace: str, groups: list[TrafficGroup]) -> dict[str, str]:
    return {
        group.service: rf"{re.escape(namespace + '-' + group.service)}-[a-z0-9]+@kubernetes(crd)?"
        for group in groups
    }


class PrometheusTraffic:
    def __init__(self, proxy, namespace: str):
        self.proxy = proxy
        self.namespace = namespace

    def _require_traefik_up(self) -> None:
        health = self.proxy.get(TelemetryBackend.PROMETHEUS, INSTANT_PATH, {"query": TRAEFIK_UP})
        results = health.get("data", {}).get("result", [])
        if not any(number(row.get("value", [None, None])[1], 0) == 1 for row in results):
            raise ProviderUnavailable("Traefik 메트릭을 아직 수집하지 못했습니다.")

    def _queries(self, patterns: dict[str, str], period: int) -> dict[str, str]:
        match = "service=~" + json.dumps("(" + "|".join(patterns.values()) + ")")
        window = f"[{period}s]"
        return {
            "counts": f"sum by (service,code) (increase({REQUESTS_COUNTER}{{{match}}}{window}))",
            "p95": f"histogram_quantile(0.95, sum by (service,le) (rate({DURATION_BUCKET}{{{match}}}{window}))) * 1000",
        }

    def _query_range(self, window: TrafficWindow, item: tuple[str, str]):
        key, expression = item
        data = self.proxy.get(
            TelemetryBackend.PROMETHEUS,
            RANGE_PATH,
            {
                "query": expression,
                "start": window.grid[0],
                "end": window.end,
                "step": window.period,
            },
        )
        if data.get("status") != "success":
            raise ProviderUnavailable("온프렘 메트릭 조회에 실패했습니다.")
        return key, data.get("data", {}).get("result", [])

    def read_series(self, groups: list[TrafficGroup], window: TrafficWindow) -> list[TrafficSeries]:
        patterns = service_patterns(self.namespace, groups)
        queries = self._queries(patterns, window.period)
        self._require_traefik_up()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = dict(pool.map(lambda item: self._query_range(window, item), queries.items()))
        return [self._series(group, patterns[group.service], results, window) for group in groups]

    @staticmethod
    def _series(
        group: TrafficGroup, pattern: str, results: dict[str, list], window: TrafficWindow
    ) -> TrafficSeries:
        totals = {t: PointTotals(at=datetime.fromtimestamp(t, UTC)) for t in window.grid}
        for kind, rows in results.items():
            for row in rows:
                if not re.fullmatch(pattern, row["metric"].get("service", "")):
                    continue
                for timestamp, raw in row["values"]:
                    point, value = totals.get(int(timestamp)), number(raw)
                    if point is None or value is None:
                        continue
                    if kind == "p95":
                        point.p95_ms = round(value, 2)
                    else:
                        point.add_count(value, str(row["metric"].get("code", "")))
        return TrafficSeries(
            color=group.color,
            label=group.label,
            target_group=group.service,
            weight=group.weight,
            points=[point.to_point() for point in totals.values()],
        )
