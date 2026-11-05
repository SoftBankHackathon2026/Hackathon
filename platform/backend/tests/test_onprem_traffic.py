from datetime import UTC, datetime
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from kubernetes.client.exceptions import ApiException

from controlplane.providers.contracts import ProviderUnavailable
from controlplane.providers.onprem.telemetry import OnpremTelemetry, traffic

NOW = datetime(2026, 10, 4, 12, 0, 30, tzinfo=UTC)
MINUTE = int(NOW.timestamp()) // 60 * 60


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(traffic, "datetime", FrozenDatetime)


def rollout(strategy):
    return {
        "metadata": {
            "labels": {
                "launchpad.io/application": "notes",
                "launchpad.io/environment": "onprem-notes",
            }
        },
        "spec": {
            "selector": {"matchLabels": {"app": "notes"}},
            "replicas": 2,
            "strategy": strategy,
        },
        "status": {
            "readyReplicas": 1,
            "phase": "Healthy",
            "blueGreen": {"activeSelector": "hash-1"},
            "canary": {"weights": {"stable": {"weight": 70}, "canary": {"weight": 30}}},
        },
    }


def pod(name, hash_value):
    return NS(
        metadata=NS(
            name=name,
            labels={
                "rollouts-pod-template-hash": hash_value,
                "launchpad.io/release": "release-" + hash_value,
            },
        ),
        status=NS(phase="Pending"),
        spec=NS(containers=[]),
    )


def reader(strategy, results, *, up=True, status="success"):
    def get(backend, path, params=None):
        if path == "api/v1/query":
            return {"data": {"result": [{"value": [MINUTE, "1" if up else "0"]}]}}
        if "histogram_quantile" in params["query"]:
            return {"status": status, "data": {"result": results["p95"]}}
        return {"status": status, "data": {"result": results["counts"]}}

    proxy = Mock()
    proxy.get.side_effect = get
    clients = Mock()
    clients.custom.get_namespaced_custom_object.return_value = rollout(strategy)
    clients.core.list_namespaced_pod.return_value = NS(
        items=[pod("a", "hash-0"), pod("b", "hash-1")]
    )
    settings = NS(
        environment=NS(
            application="notes", environment_id="onprem-notes", namespace="oneport-apps"
        ),
        observability=None,
    )
    return OnpremTelemetry(settings, "rollout", clients=clients, proxy=proxy)


BLUE_GREEN = {"blueGreen": {"activeService": "active", "previewService": "preview"}}
CANARY = {"canary": {"stableService": "stable", "canaryService": "canary"}}


def service(name, kind="kubernetes"):
    return f"oneport-apps-{name}-abc123@{kind}"


def test_blue_green_series_aggregate_counts_errors_and_p95():
    results = {
        "counts": [
            {"metric": {"service": service("active"), "code": "200"}, "values": [[MINUTE, "10"]]},
            {"metric": {"service": service("active"), "code": "503"}, "values": [[MINUTE, "2"]]},
            {"metric": {"service": service("active"), "code": "404"}, "values": [[MINUTE, "1"]]},
            {
                "metric": {"service": service("preview", "kubernetescrd"), "code": "200"},
                "values": [[MINUTE, "4"]],
            },
            {"metric": {"service": "foreign-service", "code": "200"}, "values": [[MINUTE, "99"]]},
        ],
        "p95": [{"metric": {"service": service("active")}, "values": [[MINUTE, "12.3456"]]}],
    }
    result = reader(BLUE_GREEN, results).traffic(minutes=30)
    assert result.period_seconds == 60
    active, preview = result.series
    assert (active.color, active.label, active.target_group, active.weight) == (
        "blue",
        "운영",
        "active",
        100,
    )
    assert (preview.color, preview.weight) == ("green", 0)
    assert len(active.points) == 30
    point = next(p for p in active.points if p.at.timestamp() == MINUTE)
    assert (point.requests, point.server_errors, point.client_errors) == (13, 2, 1)
    assert point.p95_ms == 12.35
    assert next(p for p in preview.points if p.at.timestamp() == MINUTE).requests == 4
    assert result.live_release == "release-hash-1"
    assert (result.running, result.desired, result.deployment_status) == (1, 2, "Healthy")


def test_canary_series_carry_the_rollout_weights():
    result = reader(CANARY, {"counts": [], "p95": []}).traffic(minutes=600)
    assert result.period_seconds == 300
    assert [(s.target_group, s.weight) for s in result.series] == [("stable", 70), ("canary", 30)]


def test_missing_rollout_reports_no_series():
    telemetry = reader(BLUE_GREEN, {"counts": [], "p95": []})
    telemetry.clients.custom.get_namespaced_custom_object.side_effect = ApiException(status=404)
    result = telemetry.traffic(minutes=15)
    assert result.series == [] and result.source == "prometheus"


def test_silent_traefik_scrape_is_unavailable():
    with pytest.raises(ProviderUnavailable, match="Traefik"):
        reader(BLUE_GREEN, {"counts": [], "p95": []}, up=False).traffic(minutes=15)


def test_failed_prometheus_query_is_unavailable():
    with pytest.raises(ProviderUnavailable, match="메트릭 조회"):
        reader(BLUE_GREEN, {"counts": [], "p95": []}, status="error").traffic(minutes=15)
