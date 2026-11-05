import copy
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
import requests
from cloud_run_fakes import CloudRunApi
from google.cloud import run_v2
from pydantic import SecretStr

from controlplane.execution.gcp.settings import GcpSettings, GcpTracingSettings
from controlplane.providers.contracts import ProviderConflict, ProviderUnavailable
from controlplane.providers.gcp.adapter import GcpAdapter
from controlplane.providers.gcp.models import GcpDeploymentRequest
from controlplane.providers.gcp.provider import CloudRunProvider, digest
from controlplane.providers.gcp.telemetry import GcpTelemetry, trace_detail
from controlplane.providers.gcp.tracing import COLLECTOR, PYTHON, PYTHON_IMAGE
from controlplane.providers.telemetry import TelemetryProvider

TRACE = "a" * 32
NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
IMAGE = "docker.io/team/app@sha256:" + "b" * 64


@pytest.fixture
def settings():
    return GcpSettings(
        environment=dict(
            application="demo",
            environment_id="gcp-demo",
            project_id="test-project",
            region="asia-northeast3",
            service="oneport-demo",
            service_uid="uid",
            service_url="https://demo.example.com",
        ),
        database_url="postgresql://unused",
        allowed_images=[IMAGE],
        tracing={"python_image": PYTHON_IMAGE},
    )


def span(sid, parent="0", *, service="oneport-demo", **labels):
    return dict(
        spanId=sid,
        parentSpanId=parent,
        name="POST /notes",
        kind="RPC_SERVER" if parent == "0" else "RPC_CLIENT",
        startTime="2026-10-03T11:59:00Z",
        endTime="2026-10-03T11:59:02Z",
        labels={
            "oneport.service": service,
            "oneport.application": "demo",
            "oneport.environment": "gcp-demo",
            "oneport.release": "c" * 24,
            "/http/method": "POST",
            "/http/url": "https://demo.example.com/notes?token=private",
            "/http/status_code": "500",
            **labels,
        },
    )


def trace(*spans):
    return {"projectId": "test-project", "traceId": TRACE, "spans": list(spans)}


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        response = Mock(status_code=200)
        response.json.return_value = result
        return response

    def close(self):
        pass


def log(service="oneport-demo", tid=TRACE):
    return {
        "resource": {
            "type": "cloud_run_revision",
            "labels": {
                "project_id": "test-project",
                "location": "asia-northeast3",
                "service_name": service,
                "revision_name": "r1",
            },
        },
        "timestamp": "2026-10-03T11:59:01Z",
        "textPayload": f"ERROR trace_id={tid} broken",
    }


def test_detail_keeps_own_span_tree_errors_sql_and_release_only(settings):
    data = trace(
        span("1"),
        span(
            "2",
            "1",
            **{
                "db.statement": "SELECT * FROM notes WHERE id = %s",
                "exception.message": "database failed",
            },
        ),
        span("3", "2", service="other-app"),
    )
    detail = trace_detail(data, settings.environment, TRACE)
    assert [(s.id, s.parent_id, s.depth) for s in detail.spans] == [("1", None, 0), ("2", "1", 1)]
    assert detail.spans[0].http_path == "/notes"
    assert detail.spans[0].fault and detail.spans[0].duration_ms == 2000
    assert detail.spans[1].sql.endswith("%s")
    assert detail.spans[1].exception == "database failed"
    assert detail.release == "c" * 24
    assert trace_detail(trace(span("3", service="other-app")), settings.environment, TRACE) is None
    data["projectId"] = "foreign-project"
    assert trace_detail(data, settings.environment, TRACE) is None


def test_detail_handles_cloud_run_roots_and_cycles(settings):
    raw = span("1")
    raw["labels"] = {
        "g.co/r/cloud_run_revision/service_name": "oneport-demo",
        "g.co/r/cloud_run_revision/location": "asia-northeast3",
    }
    assert trace_detail(trace(raw), settings.environment, TRACE)
    raw["labels"]["g.co/r/cloud_run_revision/location"] = "us-east1"
    assert trace_detail(trace(raw), settings.environment, TRACE) is None
    detail = trace_detail(trace(span("1", "2"), span("2", "1")), settings.environment, TRACE)
    assert len(detail.spans) == 2


def test_lists_paginate_filter_and_deduplicate_without_returning_other_apps(settings):
    session = Session(
        {"traces": [trace(span("1", service="other"))], "nextPageToken": "page2"},
        {"traces": [trace(span("1"))]},
        {"traces": [trace(span("1"))]},
    )
    session.responses.append({"traces": []})
    result = GcpTelemetry(settings, session, lambda: NOW).traces(minutes=60, kind="errors")
    assert len(result.traces) == 1 and result.traces[0].fault
    assert result.traces[0].path == "/notes"
    assert session.calls[1][2]["params"]["pageToken"] == "page2"
    assert "+oneport.environment:gcp-demo" in session.calls[0][2]["params"]["filter"]


def test_trace_logs_use_cloud_logging_scope_and_exact_trace_id(settings):
    session = Session(trace(span("1")), {"entries": [log(), log("other-app"), log(tid="b" * 32)]})
    detail = GcpTelemetry(settings, session).trace(TRACE)
    assert len(detail.logs) == 1
    query = session.calls[1][2]["json"]["filter"]
    assert 'resource.labels.service_name="oneport-demo"' in query
    assert 'trace="projects/test-project/traces/' + TRACE in query
    assert "token=private" not in query


def test_foreign_detail_never_reads_logs_and_bad_ids_never_call_gcp(settings):
    session = Session(trace(span("1", service="other")))
    reader = GcpTelemetry(settings, session)
    assert reader.trace(TRACE) is None
    assert len(session.calls) == 1
    with pytest.raises(ValueError):
        reader.trace("../other?query=x")
    assert len(session.calls) == 1


def test_read_errors_are_sanitized(settings):
    session = Session(requests.ConnectionError("private transport detail"))
    with pytest.raises(ProviderUnavailable, match="GCP monitoring read failed") as error:
        GcpTelemetry(settings, session).trace(TRACE)
    assert "private" not in str(error.value)


def test_metrics_match_revision_weights_and_do_not_claim_zero_servers(settings):
    def metric(value, code=None):
        return {
            "resource": {"labels": {"revision_name": "oneport-demo-v1"}},
            "metric": {"labels": {"response_code_class": code} if code else {}},
            "points": [
                {"interval": {"endTime": "2026-10-03T11:59:00Z"}, "value": {"doubleValue": value}}
            ],
        }

    session = Session(
        {"timeSeries": [metric(8, "2xx"), metric(2, "5xx")]},
        {"timeSeries": [metric(120)]},
        {"timeSeries": [metric(1)]},
        {"uid": "uid", "trafficStatuses": [{"revision": "oneport-demo-v1", "percent": 100}]},
        {"timeSeries": [metric(1)]},
        {},
        {},
    )
    result = GcpTelemetry(settings, session, lambda: NOW).traffic(minutes=15)
    assert result.source == "cloud_monitoring" and result.running == 1 and result.desired is None
    assert result.resource_usage is None
    series = result.series[0]
    point = next(p for p in series.points if p.requests)
    assert (point.requests, point.server_errors, point.p95_ms) == (10, 2, 120)
    assert series.weight == 100 and series.label == "oneport-demo-v1"
    assert 'resource.labels.service_name="oneport-demo"' in session.calls[0][2]["params"]["filter"]


@pytest.mark.parametrize("minutes", [15, 60, 180, 360])
def test_current_instances_are_independent_of_history_window(settings, minutes):
    def instances(count, revision):
        return {
            "resource": {"labels": {"revision_name": revision}},
            "points": [
                {
                    "interval": {"endTime": "2026-10-03T11:59:00Z"},
                    "value": {"int64Value": str(count)},
                }
            ],
        }

    session = Session(
        {},
        {},
        {"timeSeries": [instances(9, "old")]},
        {"uid": "uid", "trafficStatuses": [{"revision": "new", "percent": 100}]},
        {"timeSeries": [instances(0, "old"), instances(2, "new")]},
        {},
        {},
    )
    result = GcpTelemetry(settings, session, lambda: NOW).traffic(minutes=minutes)
    assert result.running == 2
    assert result.running_sampled_at.isoformat() == "2026-10-03T11:59:00+00:00"
    assert result.period_seconds == (60 if minutes <= 180 else 300)
    query = session.calls[4][2]["params"]
    assert query["aggregation.alignmentPeriod"] == "60s"
    assert query["aggregation.perSeriesAligner"] == "ALIGN_MAX"
    assert query["interval.startTime"] == "2026-10-03T11:50:00Z"
    assert query["interval.endTime"] == "2026-10-03T12:00:00Z"
    assert 'resource.labels.service_name="oneport-demo"' in query["filter"]


@pytest.mark.parametrize(
    "response,expected",
    [
        ({}, None),
        (requests.ConnectionError("unavailable"), None),
        (
            {
                "timeSeries": [
                    {
                        "points": [
                            {
                                "interval": {"endTime": "2026-10-03T11:40:00Z"},
                                "value": {"int64Value": "3"},
                            }
                        ]
                    }
                ]
            },
            None,
        ),
        (
            {
                "timeSeries": [
                    {
                        "points": [
                            {
                                "interval": {"endTime": "2026-10-03T11:59:00Z"},
                                "value": {"int64Value": "0"},
                            }
                        ]
                    }
                ]
            },
            0,
        ),
    ],
)
def test_current_instances_distinguish_zero_from_missing_stale_or_failed(
    settings, response, expected
):
    running, at = GcpTelemetry(settings, Session(response), lambda: NOW)._running(NOW)
    assert running == expected
    assert (at is None) == (expected is None)


def test_python_sidecars_keep_db_identity_and_survive_redeploy(settings):
    api = CloudRunApi(settings.environment, IMAGE)
    provider = CloudRunProvider(api, api, settings.environment, settings.tracing)
    request = GcpDeploymentRequest(
        application="demo",
        environment_id="gcp-demo",
        source={"kind": "image", "image": IMAGE},
        instrumentation="python",
        env={"PYTHONPATH": "/app/src"},
    )
    candidate = provider.candidate_template(api.service, request, "oneport-demo-next", "c" * 24)
    assert [c.name for c in candidate.containers[1:]] == [PYTHON, COLLECTOR]
    assert candidate.service_account == api.service.template.service_account
    assert candidate.vpc_access == api.service.template.vpc_access
    app = candidate.containers[0]
    values = {e.name: e.value for e in app.env}
    assert values["PYTHONPATH"].endswith(":/app/src")
    assert values["OTEL_TRACES_SAMPLER"] == "traceidratio"
    assert set(app.depends_on) == {COLLECTOR, PYTHON}
    assert (
        next(e for e in app.env if e.name == "DB_PASSWORD").value_source.secret_key_ref.secret
        == "app-db"
    )
    assert all(not c.resources.cpu_idle for c in candidate.containers)
    api.service.template = candidate
    again = provider.candidate_template(api.service, request, "oneport-demo-next", "c" * 24)
    assert digest(again) == digest(candidate)
    request.instrumentation = None
    request.env = {}
    off = provider.candidate_template(api.service, request, "oneport-demo-next2", "d" * 24)
    assert [c.name for c in off.containers[1:]] == [COLLECTOR]
    assert not off.volumes
    assert next(e.value for e in off.containers[0].env if e.name == "PYTHONPATH") == "/app/src"


def test_config_requires_registration_rejects_overrides_and_preserves_old_fingerprint(settings):
    old = settings.model_copy(update={"tracing": None})
    assert old.fingerprint() == settings.fingerprint()
    request = GcpDeploymentRequest(
        application="demo",
        environment_id="gcp-demo",
        source={"kind": "image", "image": IMAGE},
        instrumentation="python",
    )
    with pytest.raises(ValueError, match="추적 설정"):
        old.validate_request(request)
    request.env = {"OTEL_SERVICE_NAME": "other"}
    with pytest.raises(ValueError, match="managed"):
        settings.validate_request(request)
    with pytest.raises(ValueError):
        GcpTracingSettings(sample_ratio=1.1)


def test_unknown_sidecars_are_not_overwritten(settings):
    api = CloudRunApi(settings.environment, IMAGE)
    api.service.template.containers.append(run_v2.Container(name="external", image=IMAGE))
    provider = CloudRunProvider(api, api, settings.environment, settings.tracing)
    request = GcpDeploymentRequest(
        application="demo", environment_id="gcp-demo", source={"kind": "image", "image": IMAGE}
    )
    with pytest.raises(ProviderConflict):
        provider.candidate_template(api.service, request, "next", "c" * 24)


def test_gcp_supports_common_monitoring_contract():
    assert isinstance(object.__new__(GcpAdapter), TelemetryProvider)


def test_common_api_exposes_gcp_monitoring_and_python_option(settings, store, monkeypatch):
    from fastapi.testclient import TestClient

    from controlplane.main import create_app
    from controlplane.providers.registry import ProviderRegistry

    settings.database_url = SecretStr(store.dsn)
    adapter = GcpAdapter(settings, store=store)
    session_fake = Session(
        {"traces": [trace(span("1"))]},
        {"traces": []},
        {"traces": []},
        trace(span("1")),
        {"entries": [log()]},
    )
    monkeypatch.setattr("controlplane.providers.gcp.adapter.credentials", Mock())
    monkeypatch.setattr("controlplane.providers.gcp.adapter.AuthorizedSession", Mock())
    monkeypatch.setattr(
        "controlplane.providers.gcp.adapter.GcpTelemetry",
        lambda settings, session=None: GcpTelemetry(settings, session_fake, lambda: NOW),
    )
    registry = ProviderRegistry()
    registry.register(adapter)
    token = "gcp-monitoring-regression-token-123456"
    client = TestClient(create_app(registry=registry, store=store, operator_token=token))
    headers = {"Authorization": "Bearer " + token}
    result = client.get("/api/v1/environments/gcp-demo/traces", headers=headers)
    assert result.status_code == 200 and result.json()["traces"][0]["fault"]
    detail = client.get(f"/api/v1/environments/gcp-demo/traces/{TRACE}", headers=headers)
    assert detail.status_code == 200 and len(detail.json()["logs"]) == 1
    env = adapter.public_environment()
    assert env["monitoring_enabled"] and env["instrumentation_options"] == ["auto", "sdk", "python"]


def test_monitoring_reads_share_one_authorized_session(settings, monkeypatch):
    session = Mock()
    session.request.return_value = Mock(status_code=200, json=Mock(return_value={"traces": []}))
    opened = Mock(return_value=session)
    monkeypatch.setattr("controlplane.providers.gcp.adapter.credentials", Mock())
    monkeypatch.setattr("controlplane.providers.gcp.adapter.AuthorizedSession", opened)
    adapter = GcpAdapter(settings)
    for _ in range(3):
        adapter.traces(minutes=15, kind="all")
    assert opened.call_count == 1
    session.close.assert_not_called()
    session.request.side_effect = requests.ConnectionError("down")
    with pytest.raises(ProviderUnavailable):
        adapter.traces(minutes=15, kind="all")
    session.request.side_effect = None
    adapter.traces(minutes=15, kind="all")
    assert opened.call_count == 2


def test_api_and_worker_share_explicit_credentials_without_process_adc(
    settings, monkeypatch, tmp_path
):
    import google.auth

    from controlplane.execution.gcp.auth import credentials

    settings = GcpSettings.model_validate(
        {**settings.model_dump(), "credentials_file": str(tmp_path / "account.json")}
    )
    expected = object()
    load = Mock(return_value=(expected, "test-project"))
    monkeypatch.setattr(google.auth, "load_credentials_from_file", load)
    monkeypatch.setattr(google.auth, "default", Mock(side_effect=AssertionError("API has no ADC")))
    assert credentials(settings) is expected
    assert load.call_args.args == (str(tmp_path / "account.json"),)


def test_standalone_health_check_db_spans_do_not_crowd_request_list(settings):
    db = span("2", "1")
    db["labels"] = {k: v for k, v in db["labels"].items() if not k.startswith("/http/")}
    session = Session({"traces": [trace(db)]}, {"traces": []}, {"traces": []})
    result = GcpTelemetry(settings, session, lambda: NOW).traces(minutes=15, kind="all")
    assert result.traces == []
    assert "label:/http/method" in session.calls[0][2]["params"]["filter"]


@pytest.mark.parametrize(
    "kind,key",
    [("java", "JAVA_TOOL_OPTIONS"), ("nodejs", "NODE_OPTIONS"), ("dotnet", "DOTNET_STARTUP_HOOKS")],
)
def test_language_agents_redeploy_and_explicit_off_preserve_secrets(settings, kind, key):
    from controlplane.domain.instrumentation import IMAGES
    from controlplane.providers.gcp.tracing import AGENT

    settings.tracing.agent_images = dict(IMAGES)
    api = CloudRunApi(settings.environment, IMAGE)
    provider = CloudRunProvider(api, api, settings.environment, settings.tracing)
    request = GcpDeploymentRequest(
        application="demo",
        environment_id="gcp-demo",
        source={"kind": "image", "image": IMAGE},
        instrumentation=kind,
        tracing_enabled=True,
    )
    candidate = provider.candidate_template(api.service, request, "next", "c" * 24)
    assert [c.name for c in candidate.containers[1:]] == [AGENT, COLLECTOR]
    assert any(
        e.name == key and "/otel-auto-instrumentation/" in e.value
        for e in candidate.containers[0].env
    )
    api.service.template = candidate
    assert digest(provider.candidate_template(api.service, request, "next", "c" * 24)) == digest(
        candidate
    )
    request.instrumentation = None
    request.tracing_enabled = False
    off = provider.candidate_template(api.service, request, "next2", "d" * 24)
    assert len(off.containers) == 1 and not off.volumes
    assert (
        next(
            e for e in off.containers[0].env if e.name == "DB_PASSWORD"
        ).value_source.secret_key_ref.secret
        == "app-db"
    )
    assert next(e.value for e in off.containers[0].env if e.name == "OTEL_SDK_DISABLED") == "true"
    assert not any("/otel-auto-instrumentation/" in e.value for e in off.containers[0].env)


def distribution(mean, count, *, service="oneport-demo", at="2026-10-03T11:59:00Z"):
    return {
        "resource": {
            "labels": {
                "project_id": "test-project",
                "location": "asia-northeast3",
                "service_name": service,
            }
        },
        "points": [
            {
                "interval": {"endTime": at},
                "value": {"distributionValue": {"mean": mean, "count": str(count)}},
            }
        ],
    }


def test_resource_distributions_are_weighted_app_scoped_and_fresh(settings):
    cpu = [distribution(0.1, 1), distribution(0.5, 3), distribution(9, 99, service="foreign")]
    cpu.append(distribution(0.9, 10, at="2026-10-03T11:50:00Z"))
    session = Session({"timeSeries": cpu}, {"timeSeries": [distribution(0, 4)]})
    result = GcpTelemetry(settings, session, lambda: NOW)._resources(NOW)
    assert result["cpu_percent"] == 40 and result["memory_percent"] == 0
    assert result["cpu_sampled_at"].isoformat() == "2026-10-03T11:59:00+00:00"
    query = session.calls[0][2]["params"]
    assert query["aggregation.crossSeriesReducer"] == "REDUCE_NONE"
    assert 'resource.labels.service_name="oneport-demo"' in query["filter"]


def test_missing_stale_and_failed_resources_are_not_reported_as_zero(settings):
    session = Session({"timeSeries": [distribution(0.5, 3, at="2026-10-03T11:40:00Z")]}, {})
    assert GcpTelemetry(settings, session, lambda: NOW)._resources(NOW) is None
    session = Session(
        requests.ConnectionError("unavailable"), {"timeSeries": [distribution(0.25, 3)]}
    )
    result = GcpTelemetry(settings, session, lambda: NOW)._resources(NOW)
    assert result.get("cpu_percent") is None and result["memory_percent"] == 25


def test_new_rollouts_default_to_automatic_but_saved_manual_policy_survives():
    from controlplane.domain.models import RolloutPolicy

    assert RolloutPolicy().promotion == "automatic"
    policy = RolloutPolicy(promotion="manual", auto_rollback=False)
    assert RolloutPolicy.model_validate_json(policy.model_dump_json()) == policy
