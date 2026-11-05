import base64
from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from kubernetes.client.exceptions import ApiException

from controlplane.main import create_app
from controlplane.providers.contracts import ProviderUnavailable
from controlplane.providers.onprem.observability import (
    ObservabilitySettings,
    instrumentation_values,
)
from controlplane.providers.onprem.telemetry import OnpremTelemetry, trace_detail
from controlplane.providers.registry import ProviderRegistry
from controlplane.services.deployments import DeploymentService

TRACE = "a" * 32


def attr(key, value):
    return {"key": key, "value": {"stringValue": value}}


def payload():
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        attr("oneport.application", "notes"),
                        attr("oneport.environment", "onprem-notes"),
                        attr("oneport.release", "release-1"),
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": base64.b64encode(bytes.fromhex(TRACE)).decode(),
                                "spanId": base64.b64encode(bytes.fromhex("b" * 16)).decode(),
                                "name": "GET /notes",
                                "kind": 2,
                                "startTimeUnixNano": "1791000000000000000",
                                "endTimeUnixNano": "1791000000125000000",
                                "attributes": [
                                    attr("http.method", "GET"),
                                    attr("http.target", "/notes?private=value"),
                                    attr("http.status_code", "200"),
                                    attr("db.statement", "private SQL"),
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }


def test_tempo_otlp_tree_is_app_scoped_and_omits_query_and_sql_text():
    detail = trace_detail(payload(), "notes", "onprem-notes", TRACE)
    assert detail.duration_ms == 125
    assert detail.release == "release-1"
    assert detail.spans[0].http_path == "/notes"
    assert detail.spans[0].http_status == 200
    assert detail.spans[0].sql is None
    assert trace_detail(payload(), "other-app", "onprem-notes", TRACE) is None
    assert trace_detail(payload(), "notes", "other-env", TRACE) is None
    mixed = deepcopy(payload())
    foreign = deepcopy(mixed["resourceSpans"][0])
    foreign["resource"]["attributes"][0] = attr("oneport.application", "foreign")
    mixed["resourceSpans"].append(foreign)
    assert len(trace_detail(mixed, "notes", "onprem-notes", TRACE).spans) == 1


def test_trace_query_is_scoped_and_arbitrary_ids_never_reach_proxy():
    proxy = Mock()
    proxy.get.return_value = {"traces": []}
    settings = NS(environment=NS(application="notes", environment_id="onprem-notes"))
    reader = OnpremTelemetry(settings, "rollout", clients=Mock(), proxy=proxy)
    assert reader.traces(minutes=15, kind="slow").traces == []
    q = proxy.get.call_args.args[2]["q"]
    assert 'resource.oneport.application = "notes"' in q
    assert 'resource.oneport.environment = "onprem-notes"' in q
    assert "duration > 1s" in q
    proxy.reset_mock()
    with pytest.raises(ValueError):
        reader.trace("../search")
    proxy.get.assert_not_called()


def search_row(trace_id, code=None, *, legacy=False):
    span_set = {"spans": [{"attributes": [] if code is None else [attr("http.status_code", code)]}]}
    return {
        "traceID": trace_id,
        "rootTraceName": "GET /notes",
        "startTimeUnixNano": "1791000000000000000",
        "durationMs": 1500,
        **({"spanSet": span_set} if legacy else {"spanSets": [span_set]}),
    }


@pytest.mark.parametrize("kind", ["all", "slow"])
def test_trace_list_retains_failed_child_spans_and_http_failure_classes(kind):
    failed, throttled, non_http, healthy = [c * 32 for c in "abcd"]
    proxy = Mock()

    def search(backend, path, params):
        query = params["q"]
        assert 'resource.oneport.application = "notes"' in query
        assert 'resource.oneport.environment = "onprem-notes"' in query
        if "trace:id" not in query:
            # The one sampled span is healthy even though a short child failed.
            assert ("duration > 1s" in query) == (kind == "slow")
            assert "most_recent=true" in query
            return {
                "traces": [search_row(t, 200) for t in (failed, throttled, healthy)]
                + [search_row(non_http)]
            }
        assert params["limit"] == 4
        assert all(f'trace:id = "{t}"' in query for t in (failed, throttled, non_http, healthy))
        # The failure lookup must include short failed children of slow spans.
        assert "duration > 1s" not in query
        if "status = error" in query:
            return {
                "traces": [
                    search_row(failed, 500),
                    search_row(throttled, 429),
                    search_row(non_http),
                    search_row("f" * 32, 500),
                ]
            }
        if ">= 500" in query:
            return {"traces": [search_row(failed, 500)]}
        assert "= 429" in query
        return {"traces": [search_row(throttled, 429, legacy=True)]}

    proxy.get.side_effect = search
    reader = OnpremTelemetry(
        NS(environment=NS(application="notes", environment_id="onprem-notes")),
        "rollout",
        clients=Mock(),
        proxy=proxy,
    )
    rows = {t.id: t for t in reader.traces(minutes=30, kind=kind).traces}
    assert set(rows) == {failed, throttled, non_http, healthy}
    assert rows[failed].error and rows[failed].fault and rows[failed].status == 500
    assert rows[throttled].error and rows[throttled].throttle and rows[throttled].status == 429
    assert not rows[throttled].fault
    assert rows[non_http].error and rows[non_http].status is None
    assert not rows[non_http].fault and not rows[non_http].throttle
    assert not rows[healthy].error and rows[healthy].status == 200
    assert proxy.get.call_count == 4


def test_failed_error_lookup_does_not_report_successful_traces():
    proxy = Mock()

    def search(backend, path, params):
        if "trace:id" in params["q"]:
            raise ProviderUnavailable("Tempo unavailable")
        return {"traces": [search_row(TRACE, 200)]}

    proxy.get.side_effect = search
    reader = OnpremTelemetry(
        NS(environment=NS(application="notes", environment_id="onprem-notes")),
        "rollout",
        clients=Mock(),
        proxy=proxy,
    )
    with pytest.raises(ProviderUnavailable):
        reader.traces(minutes=30, kind="all")


@pytest.mark.parametrize("status_attribute", ["http.status_code", "http.response.status_code"])
@pytest.mark.parametrize("status", [404, 429, 503])
def test_error_search_includes_http_failures_with_unset_otel_status(status_attribute, status):
    row = search_row(TRACE)
    row["spanSets"][0]["spans"][0]["attributes"] = [
        {"key": status_attribute, "value": {"intValue": str(status)}}
    ]
    proxy = Mock()

    def search(backend, path, params):
        query = params["q"]
        assert 'resource.oneport.application = "notes"' in query
        assert 'resource.oneport.environment = "onprem-notes"' in query
        if "trace:id" not in query:
            # Tempo cannot return this trace if selection requires an OTel error status.
            if f"span.{status_attribute} >= 400" not in query:
                return {"traces": []}
            assert "&& (status = error ||" in query
            return {"traces": [row]}
        matches = (
            "status = error" in query
            or (">= 500" in query and status >= 500)
            or ("= 429" in query and status == 429)
        )
        return {"traces": [row] if matches else []}

    proxy.get.side_effect = search
    reader = OnpremTelemetry(
        NS(environment=NS(application="notes", environment_id="onprem-notes")),
        "rollout",
        clients=Mock(),
        proxy=proxy,
    )
    traces = reader.traces(minutes=30, kind="errors").traces
    assert len(traces) == 1
    assert traces[0].error and traces[0].status == status
    assert traces[0].fault == (status >= 500)
    assert traces[0].throttle == (status == 429)


@pytest.mark.parametrize("endpoint", ["traffic", "logs"])
def test_pod_list_failure_is_translated_to_api_503(endpoint, store, monkeypatch):
    clients = Mock()
    clients.custom.get_namespaced_custom_object.return_value = {
        "metadata": {
            "labels": {
                "launchpad.io/application": "notes",
                "launchpad.io/environment": "onprem-notes",
            }
        },
        "spec": {"selector": {"matchLabels": {"app": "notes"}}},
    }
    clients.core.list_namespaced_pod.side_effect = ApiException(
        status=503, reason="private upstream"
    )
    reader = OnpremTelemetry(
        NS(
            environment=NS(
                application="notes", environment_id="onprem-notes", namespace="oneport-apps"
            )
        ),
        "rollout",
        clients=clients,
        proxy=Mock(),
    )
    monkeypatch.setattr(DeploymentService, "telemetry", lambda *args: reader)
    monkeypatch.setattr(DeploymentService, "log_provider", lambda *args: reader)
    with TestClient(
        create_app(registry=ProviderRegistry(), store=store, auth_mode="demo"),
        raise_server_exceptions=False,
    ) as client:
        response = client.get(f"/api/v1/environments/onprem-notes/{endpoint}")
    assert response.status_code == 503
    assert "private upstream" not in response.text


def test_python_instrumentation_is_opt_in_and_identity_settings_are_reserved():
    settings = NS(
        observability=ObservabilitySettings(python_image="registry/python@sha256:" + "a" * 64)
    )
    request = NS(instrumentation=None, tracing_enabled=None, env={})
    assert instrumentation_values(settings, request, "release") is None
    request.instrumentation = "python"
    values = instrumentation_values(settings, request, "release")
    assert values["python"] and values["endpoint"].endswith(".svc.cluster.local:4318")
    request.env = {"OTEL_SERVICE_NAME": "other-app"}
    with pytest.raises(ValueError, match="managed"):
        instrumentation_values(settings, request, "release")


def test_app_resources_exclude_sidecars_and_use_limits_and_sample_timestamp():
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    pod = NS(
        metadata=NS(name="app-1"),
        status=NS(phase="Running"),
        spec=NS(
            containers=[
                NS(name="app", resources=NS(limits={"cpu": "500m", "memory": "512Mi"})),
                NS(name="sidecar", resources=NS(limits={"cpu": "1", "memory": "1Gi"})),
            ]
        ),
    )
    data = {
        "items": [
            {
                "metadata": {"name": "app-1"},
                "timestamp": now.isoformat(),
                "containers": [
                    {"name": "app", "usage": {"cpu": "250m", "memory": "128Mi"}},
                    {"name": "sidecar", "usage": {"cpu": "1", "memory": "1Gi"}},
                ],
            }
        ]
    }
    clients = Mock()
    clients.custom.list_namespaced_custom_object.return_value = data
    reader = OnpremTelemetry(
        NS(environment=NS(namespace="apps")), "rollout", clients=clients, proxy=Mock()
    )
    result = reader.resource_usage([pod])
    assert result.cpu_percent == 50 and result.memory_percent == 25
    assert result.memory_bytes == 128 * 1024**2
    assert result.cpu_sampled_at == now and result.scope == "app_containers"
    pod.spec.containers[0].resources.limits = {}
    result = reader.resource_usage([pod])
    assert result.cpu_percent is None and result.memory_bytes == 128 * 1024**2
    data["items"][0]["timestamp"] = (now - timedelta(minutes=10)).isoformat()
    assert reader.resource_usage([pod]) is None
