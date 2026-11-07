import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from kubernetes import client

from controlplane.cost.onprem import OnPremReading, register_meter
from controlplane.domain.fleet import EnvironmentConfig
from controlplane.integrations.kube import api_client

LABEL_SELECTOR = "oneport.io/cost-reading=node"
DEFAULT_NAMESPACE = "oneport-cost"
DATA_KEY = "reading.json"
DEFAULT_MAX_AGE_SECONDS = 600
METHOD_NOTES = {
    "model": "CPU 사용률 기반 추정",
    "host": "호스트 전력 모델 추정",
    "rapl": "RAPL 전력 측정",
}

ConfigMapLister = Callable[[EnvironmentConfig, str], list[dict[str, str]]]


class NoCostReadings(Exception):
    pass


class StaleCostReadings(Exception):
    pass


def list_readings(env: EnvironmentConfig, namespace: str) -> list[dict[str, str]]:
    items = (
        client.CoreV1Api(api_client(env))
        .list_namespaced_config_map(namespace, label_selector=LABEL_SELECTOR)
        .items
    )
    return [item.data or {} for item in items]


def _parse(data: dict[str, str]) -> dict[str, Any] | None:
    try:
        reading = json.loads(data[DATA_KEY])
        reading["updated_at"] = datetime.fromisoformat(reading["updated_at"])
        float(reading["average_watts"])
        float(reading["power_watts"])
        return reading
    except (KeyError, TypeError, ValueError):
        return None


@register_meter("cost-exporter")
class CostExporterMeter:
    def __init__(
        self,
        options: dict[str, Any],
        lister: ConfigMapLister = list_readings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._namespace = str(options.get("namespace", DEFAULT_NAMESPACE))
        self._max_age = timedelta(
            seconds=float(options.get("max_age_seconds", DEFAULT_MAX_AGE_SECONDS))
        )
        self._field = "power_watts" if options.get("use") == "current" else "average_watts"
        self._lister = lister
        self._clock = clock

    def read(self, env: EnvironmentConfig) -> OnPremReading:
        parsed = [r for r in map(_parse, self._lister(env, self._namespace)) if r is not None]
        if not parsed:
            raise NoCostReadings(self._namespace)
        cutoff = self._clock() - self._max_age
        fresh = sorted(
            (r for r in parsed if r["updated_at"] >= cutoff), key=lambda r: str(r.get("node"))
        )
        if not fresh:
            raise StaleCostReadings(self._namespace)
        methods = sorted({str(r.get("method")) for r in fresh})
        virtual = sum(1 for r in fresh if r.get("virtual"))
        parts = [f"노드 {len(fresh)}대"]
        if virtual:
            parts.append(f"VM {virtual}대")
        parts += [METHOD_NOTES.get(m, m) for m in methods]
        stale = len(parsed) - len(fresh)
        if stale:
            parts.append(f"오래된 값 {stale}개 제외")
        return OnPremReading(
            source="cost-exporter",
            observed_at=min(r["updated_at"] for r in fresh),
            power_watts=round(sum(float(r[self._field]) for r in fresh), 1),
            note=", ".join(parts),
        )
