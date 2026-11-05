"""Monitoring reads: collector sidecar injection, ALB traffic series, X-Ray traces
scoped to the app, and log lines joined by trace ID."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from botocore.session import get_session
from botocore.validate import validate_parameters

from controlplane.providers.contracts import ReleaseKey
from oneport_aws.providers.oneport_ecs import OneportEcsProvider, telemetry, traces

R1 = ReleaseKey(value="1" * 24)
TRACE = "1-66000010-" + "b" * 24


def deploy(provider, aws, request):
    routing = provider.initialize(request)
    td = provider.register_task(request, R1, delay=0)
    provider.submit(request, R1, td, routing)
    aws.services["shop"].update(
        desiredCount=1,
        runningCount=1,
        pendingCount=0,
        deployments=[{"status": "PRIMARY", "rolloutState": "COMPLETED"}],
    )
    return routing


# ---------------------------------------------------------------- sidecar


def test_tracing_adds_collector_sidecar_and_otel_settings(aws, environment, make_request):
    traced = environment.model_copy(update={"tracing": "xray", "trace_sample_ratio": 0.25})
    provider = OneportEcsProvider(aws, traced)
    request = make_request(env={"OTEL_TRACES_SAMPLER_ARG": "1.0"})
    provider.initialize(request)
    td = aws.task_definitions[provider.register_task(request, R1, delay=0)]
    app, collector = td["containerDefinitions"]

    names = [item["name"] for item in app["environment"]]
    env = {item["name"]: item["value"] for item in app["environment"]}
    assert env["OTEL_SERVICE_NAME"] == "shop"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4318"
    assert f"service.version={R1.value}" in env["OTEL_RESOURCE_ATTRIBUTES"]
    assert env["OTEL_PROPAGATORS"].startswith("xray")
    # The app's own value wins and is not duplicated.
    assert env["OTEL_TRACES_SAMPLER_ARG"] == "1.0"
    assert names.count("OTEL_TRACES_SAMPLER_ARG") == 1

    assert collector["name"] == "otel" and collector["essential"] is False
    assert "@sha256:" in collector["image"]
    config = collector["environment"][0]["value"]
    assert "awsxray" in config and "127.0.0.1:4318" in config and "ap-northeast-2" in config
    assert collector["logConfiguration"]["options"]["awslogs-stream-prefix"] == "shop"

    shape = (
        get_session().get_service_model("ecs").operation_model("RegisterTaskDefinition").input_shape
    )
    validate_parameters(provider.task_payload(request, R1, "X86_64"), shape)


def test_tracing_is_off_unless_the_environment_enables_it(aws, provider, make_request):
    request = make_request()
    provider.initialize(request)
    td = aws.task_definitions[provider.register_task(request, R1, delay=0)]
    assert len(td["containerDefinitions"]) == 1
    names = {item["name"] for item in td["containerDefinitions"][0]["environment"]}
    assert not any(name.startswith("OTEL_") for name in names)


# ---------------------------------------------------------------- traffic


def test_traffic_series_follow_both_target_groups(aws, provider, make_request):
    routing = deploy(provider, aws, make_request())
    blue = routing["blue"][routing["blue"].index("targetgroup/") :]
    at = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=3)
    aws.metric_points[("RequestCount", blue)] = [(at, 120.0)]
    aws.metric_points[("HTTPCode_Target_5XX_Count", blue)] = [(at, 6.0)]
    aws.metric_points[("TargetResponseTime", blue)] = [(at, 0.25)]

    result = telemetry.traffic(provider, minutes=10)
    series = {s.color: s for s in result.series}
    assert result.period_seconds == 60 and len(series["blue"].points) == 10
    point = next(p for p in series["blue"].points if p.at == at)
    assert (point.requests, point.server_errors, point.p95_ms) == (120.0, 6.0, 250.0)
    assert series["blue"].weight == 100 and series["green"].weight == 0
    assert sum(p.requests for p in series["green"].points) == 0
    assert all(p.p95_ms is None for p in series["green"].points)
    assert result.live_release == R1.value and result.running == 1


def test_traffic_before_the_first_deploy_is_empty(provider):
    result = telemetry.traffic(provider, minutes=30)
    assert result.series == [] and result.live_release is None


# ---------------------------------------------------------------- traces


def test_trace_list_is_scoped_to_the_app_and_newest_first(aws, provider):
    aws.trace_summaries = [
        {
            "Id": "1-66000000-" + "a" * 24,
            "Duration": 0.5,
            "HasFault": True,
            "Http": {
                "HttpMethod": "POST",
                "HttpURL": "https://shop.app.yubin.dev/api/notes?token=x",
                "HttpStatus": 500,
            },
        },
        {"Id": TRACE, "Duration": 0.012, "Http": {"HttpMethod": "GET", "HttpStatus": 200}},
    ]
    rows = traces.traces(provider, minutes=30, kind="errors")
    assert aws.last_trace_filter == 'service("shop") AND (error OR fault)'
    assert [row.id for row in rows] == [TRACE, "1-66000000-" + "a" * 24]
    assert rows[1].path == "/api/notes" and rows[1].fault and rows[1].status == 500
    assert rows[1].started_at == datetime.fromtimestamp(0x66000000, UTC)
    assert rows[0].duration_ms == 12.0


def _segment(name, sid, start, end, **fields):
    return {
        "Id": sid,
        "Document": json.dumps(
            {"name": name, "id": sid, "start_time": start, "end_time": end, **fields}
        ),
    }


def test_trace_detail_builds_the_span_tree_and_joins_logs(aws, provider):
    start = 1_790_000_000.0
    root = {
        "fault": True,
        "http": {
            "request": {"method": "POST", "url": "https://shop.app.yubin.dev/api/notes?q=1"},
            "response": {"status": 500},
        },
        "service": {"version": R1.value},
        "subsegments": [
            {
                "id": "sub-db",
                "name": "notes@db",
                "namespace": "remote",
                "start_time": start + 0.01,
                "end_time": start + 0.31,
                "sql": {"sanitized_query": "INSERT INTO notes (title) VALUES (%s)"},
            },
            {
                "id": "sub-handler",
                "name": "POST /api/notes",
                "start_time": start + 0.32,
                "end_time": start + 0.4,
                "fault": True,
                "cause": {"exceptions": [{"type": "RuntimeError", "message": "injected"}]},
            },
        ],
    }
    aws.traces = [
        {"Id": TRACE, "Segments": [_segment("shop", "seg-root", start, start + 0.5, **root)]}
    ]
    hex_id = TRACE[2:10] + TRACE[11:]
    aws.log_events = [
        {
            "timestamp": int(start * 1000) + 300,
            "logStreamName": "shop/app/t1",
            "message": json.dumps({"message": "boom", "trace_id": hex_id}),
        },
        {
            "timestamp": int(start * 1000) + 310,
            "logStreamName": "shop/app/t1",
            "message": json.dumps({"message": "other request", "trace_id": "c" * 32}),
        },
    ]

    found = traces.trace(provider, TRACE)
    spans = found.spans
    assert [(s.name, s.depth) for s in spans] == [
        ("shop", 0),
        ("notes@db", 1),
        ("POST /api/notes", 1),
    ]
    assert spans[0].start_ms == 0 and spans[0].duration_ms == 500.0
    assert spans[0].http_path == "/api/notes" and spans[0].http_status == 500
    assert spans[1].sql.startswith("INSERT") and spans[1].parent_id == "seg-root"
    assert spans[2].exception == "injected"
    assert found.release == R1.value
    assert [json.loads(line.message)["message"] for line in found.logs] == ["boom"]


def test_trace_of_another_app_or_bad_id_is_not_returned(aws, provider):
    aws.traces = [{"Id": TRACE, "Segments": [_segment("blog", "s1", 1.0, 2.0)]}]
    assert traces.trace(provider, TRACE) is None
    assert traces.trace(provider, "1-66000010-" + "d" * 24) is None
    with pytest.raises(ValueError):
        traces.trace(provider, 'x") OR service("blog')


def test_python_instrumentation_adds_init_container_and_pythonpath(aws, environment, make_request):
    traced = environment.model_copy(
        update={
            "tracing": "xray",
            "python_autoinstrumentation_image": "registry/otel-python@sha256:1",
        }
    )
    provider = OneportEcsProvider(aws, traced)
    request = make_request(instrumentation="python", env={"PYTHONPATH": "/app/src"})
    provider.initialize(request)
    payload = provider.task_payload(request, R1, "ARM64")
    app, init, collector = payload["containerDefinitions"]
    env = {item["name"]: item["value"] for item in app["environment"]}

    assert init["name"] == "otel-python-init" and init["essential"] is False
    assert app["dependsOn"] == [{"containerName": "otel-python-init", "condition": "SUCCESS"}]
    assert payload["volumes"] == [{"name": "otel-python"}]
    assert app["mountPoints"][0]["readOnly"] is True
    assert env["PYTHONPATH"].endswith(":/app/src")
    assert env["PYTHONPATH"].startswith(
        "/otel-auto-instrumentation-python/opentelemetry/instrumentation/auto_instrumentation:"
    )
    assert env["OTEL_PYTHON_ID_GENERATOR"] == "xray"
    assert env["OTEL_PYTHON_EXCLUDED_URLS"] == "health"
    assert [i["name"] for i in app["environment"]].count("PYTHONPATH") == 1
    assert collector["name"] == "otel"
    shape = (
        get_session().get_service_model("ecs").operation_model("RegisterTaskDefinition").input_shape
    )
    validate_parameters(payload, shape)


def test_python_instrumentation_needs_tracing_and_the_init_image(aws, environment, make_request):
    request = make_request(instrumentation="python")
    for update in ({}, {"tracing": "xray"}):
        provider = OneportEcsProvider(aws, environment.model_copy(update=update))
        with pytest.raises(ValueError, match="자동 계측"):
            provider.validate(request)


@pytest.mark.parametrize(
    "kind,key",
    [("java", "JAVA_TOOL_OPTIONS"), ("nodejs", "NODE_OPTIONS"), ("dotnet", "DOTNET_STARTUP_HOOKS")],
)
def test_runtime_agent_uses_success_dependency_and_w3c_export(
    aws, environment, make_request, kind, key
):
    provider = OneportEcsProvider(aws, environment.model_copy(update={"tracing": "xray"}))
    request = make_request(instrumentation=kind, tracing_enabled=True)
    payload = provider.task_payload(request, R1, "X86_64")
    app = payload["containerDefinitions"][0]
    values = {e["name"]: e["value"] for e in app["environment"]}
    assert "/otel-auto-instrumentation/" in values[key]
    assert values["OTEL_PROPAGATORS"] == "tracecontext,baggage"
    assert {"containerName": "otel-agent-init", "condition": "SUCCESS"} in app["dependsOn"]
    shape = (
        get_session().get_service_model("ecs").operation_model("RegisterTaskDefinition").input_shape
    )
    validate_parameters(payload, shape)
    request.tracing_enabled = False
    request.instrumentation = None
    payload = provider.task_payload(request, R1, "X86_64")
    assert len(payload["containerDefinitions"]) == 1


def test_service_resources_use_latest_fresh_sample_and_keep_zero(aws, provider, make_request):
    deploy(provider, aws, make_request())
    at = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=2)
    aws.metric_points[("CPUUtilization", "shop")] = [(at, 0.0), (at - timedelta(minutes=8), 99.0)]
    aws.metric_points[("MemoryUtilization", "shop")] = [(at, 42.5)]
    result = telemetry.traffic(provider, minutes=360).resource_usage
    assert result.cpu_percent == 0.0 and result.memory_percent == 42.5
    assert result.cpu_sampled_at == at and result.sample_window_seconds == 60
    aws.metric_points[("CPUUtilization", "shop")] = [(at - timedelta(minutes=8), 99.0)]
    aws.metric_points[("MemoryUtilization", "shop")] = []
    assert telemetry.traffic(provider, minutes=360).resource_usage is None
