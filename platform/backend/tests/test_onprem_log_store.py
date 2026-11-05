from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from controlplane.main import create_app
from controlplane.providers.contracts import ProviderUnavailable
from controlplane.providers.interface import AppLogs
from controlplane.providers.onprem.observability import ObservabilitySettings, TelemetryProxy
from controlplane.providers.onprem.telemetry import OnpremTelemetry
from controlplane.providers.registry import ProviderRegistry
from controlplane.services.deployments import DeploymentService

NOW = datetime(2026, 10, 4, tzinfo=UTC)
LABELS = {
    "namespace": "oneport-apps",
    "application": "notes",
    "environment": "onprem-notes",
    "container": "app",
}


def reader(results):
    proxy, clients = Mock(), Mock()
    proxy.get.return_value = {
        "status": "success",
        "data": {"resultType": "streams", "result": results},
    }
    settings = NS(
        environment=NS(
            namespace="oneport-apps", application="notes", environment_id="onprem-notes"
        ),
        observability=ObservabilitySettings(logs_backend="loki"),
    )
    return OnpremTelemetry(settings, "rollout", clients=clients, proxy=proxy)


def stream(pod, messages, **labels):
    return {
        "stream": {**LABELS, "pod": pod, **labels},
        "values": [[str(int(NOW.timestamp() * 1e9) + i), text] for i, text in messages],
    }


def test_retained_logs_survive_missing_pods_and_filter_foreign_streams():
    query = 'trace_id="abc" \\ marker'
    r = reader(
        [
            stream("deleted-old-pod", [(0, query + " old")]),
            stream("current-pod", [(1000, query + " new")]),
            stream("foreign-pod", [(2000, query + " secret")], application="foreign"),
            stream("foreign-env", [(3000, query + " secret")], environment="foreign"),
            stream("db-pod", [(4000, query + " secret")], container="postgres"),
        ]
    )
    result = r.app_logs(
        minutes=30, filter_text=query, limit=10, start=NOW, end=NOW + timedelta(seconds=1)
    )
    assert [line.stream for line in result.lines] == ["deleted-old-pod", "current-pod"]
    r.clients.custom.get_namespaced_custom_object.assert_not_called()
    r.clients.core.list_namespaced_pod.assert_not_called()
    params = r.proxy.get.call_args.args[2]
    assert 'application="notes"' in params["query"]
    assert 'environment="onprem-notes"' in params["query"]
    assert '\\"abc\\"' in params["query"]
    assert params["direction"] == "backward" and params["limit"] == 11


def test_latest_lines_are_chronological_and_truncation_is_reported():
    r = reader([stream("old-pod", [(2000, "newest"), (1000, "middle"), (0, "oldest")])])
    result = r.app_logs(
        minutes=30, filter_text=None, limit=2, start=NOW, end=NOW + timedelta(seconds=1)
    )
    assert [line.message for line in result.lines] == ["middle", "newest"]
    assert result.truncated


def test_store_failure_is_not_disguised_as_empty_or_live_only_history():
    r = reader([])
    r.proxy.get.side_effect = ProviderUnavailable("unavailable")
    with pytest.raises(ProviderUnavailable):
        r.app_logs(minutes=30, filter_text=None, limit=10)
    r.clients.core.list_namespaced_pod.assert_not_called()


def test_legacy_setting_serialization_and_read_only_proxy_boundary():
    assert "logs_backend" not in ObservabilitySettings().model_dump()
    assert ObservabilitySettings(logs_backend="loki").model_dump()["logs_backend"] == "loki"
    proxy = TelemetryProxy(Mock(), ObservabilitySettings())
    with pytest.raises(ValueError):
        proxy.get("loki", "loki/api/v1/push")


@pytest.mark.parametrize("maximum,requested,status", [(10080, 10080, 200), (360, 1440, 422)])
def test_api_retained_window_does_not_expand_other_providers(
    store, monkeypatch, maximum, requested, status
):
    provider = NS(
        max_log_minutes=maximum,
        app_logs=Mock(
            return_value=AppLogs(
                application="notes",
                environment_id="onprem-notes",
                minutes=requested,
                max_lookback_minutes=maximum,
                lines=[],
            )
        ),
    )
    monkeypatch.setattr(DeploymentService, "log_provider", lambda *args: provider)
    with TestClient(
        create_app(registry=ProviderRegistry(), store=store, auth_mode="demo")
    ) as client:
        response = client.get(f"/api/v1/environments/onprem-notes/logs?minutes={requested}")
    assert response.status_code == status
    if status == 200:
        assert response.json()["max_lookback_minutes"] == 10080
    else:
        provider.app_logs.assert_not_called()
