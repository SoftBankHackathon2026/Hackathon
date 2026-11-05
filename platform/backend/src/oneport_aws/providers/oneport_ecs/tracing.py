"""OpenTelemetry collector sidecar for X-Ray (infra ONEPORT_TRACING=xray).

The app exports OTLP to localhost; the collector sends traces to X-Ray with the
task role (infra runtime policy `WriteTraces`). Apps without an OpenTelemetry SDK
ignore the variables, and the collector only idles, unless the release asks for
zero-code instrumentation: for `python` an init container copies the
OpenTelemetry packages to a shared volume and the app loads them through
PYTHONPATH (sitecustomize), as the OpenTelemetry Operator does on Kubernetes.
"""

from controlplane.domain.instrumentation import (
    IMAGES,
    PATH,
    agent_environment,
    copy_command,
    language,
)
from controlplane.domain.models import DeploymentRequest
from oneport_aws.providers.oneport_ecs.models import OneportEcsEnvironment

COLLECTOR = "otel"
PYTHON_INIT = "otel-python-init"
PYTHON_VOLUME = "otel-python"
PYTHON_PATH = "/otel-auto-instrumentation-python"

# Bound to localhost: containers in an awsvpc task share it, nothing else can reach it.
_CONFIG = """receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 127.0.0.1:4317
      http:
        endpoint: 127.0.0.1:4318
processors:
  batch:
    timeout: 1s
exporters:
  awsxray:
    region: {region}
service:
  telemetry:
    logs:
      level: warn
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [awsxray]
"""


def app_environment(env: OneportEcsEnvironment, release: str) -> dict[str, str]:
    """Standard OTEL_* settings. The X-Ray propagator continues the ALB trace header."""
    return {
        "OTEL_SDK_DISABLED": "false",
        "OTEL_SERVICE_NAME": env.application,
        "OTEL_RESOURCE_ATTRIBUTES": (
            f"service.version={release},deployment.environment.name={env.environment_id}"
        ),
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_PROPAGATORS": "xray,tracecontext,baggage",
        "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
        "OTEL_TRACES_SAMPLER_ARG": str(env.trace_sample_ratio),
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
    }


def sidecar(env: OneportEcsEnvironment, log_options: dict) -> dict:
    return {
        "name": COLLECTOR,
        "image": env.otel_collector_image,
        # Not essential: a collector failure must not stop the app.
        "essential": False,
        "memoryReservation": 64,
        "environment": [{"name": "AOT_CONFIG_CONTENT", "value": _CONFIG.format(region=env.region)}],
        "logConfiguration": {"logDriver": "awslogs", "options": dict(log_options)},
    }


def instrumentation_options(env: OneportEcsEnvironment) -> list[str]:
    if env.tracing != "xray":
        return []
    return ["auto", "java", "nodejs", "dotnet", "sdk"] + (
        ["python"] if env.python_autoinstrumentation_image else []
    )


def python_environment(request: DeploymentRequest, user_path: str | None) -> dict[str, str]:
    path = f"{PYTHON_PATH}/opentelemetry/instrumentation/auto_instrumentation:{PYTHON_PATH}"
    return {
        "PYTHONPATH": f"{path}:{user_path}" if user_path else path,
        "OTEL_TRACES_EXPORTER": "otlp",
        # X-Ray needs trace IDs that start with the epoch second.
        "OTEL_PYTHON_ID_GENERATOR": "xray",
        # Adds otelTraceID/otelSpanID to every log record; the platform joins logs by it.
        "OTEL_PYTHON_LOG_CORRELATION": "true",
        "OTEL_PYTHON_EXCLUDED_URLS": request.health_path.strip("/") or "healthz",
    }


def _add_python(payload: dict, env: OneportEcsEnvironment, request: DeploymentRequest) -> None:
    app = payload["containerDefinitions"][0]
    user_path = next((i["value"] for i in app["environment"] if i["name"] == "PYTHONPATH"), None)
    app["environment"] = [i for i in app["environment"] if i["name"] != "PYTHONPATH"]
    present = {item["name"] for item in app["environment"]}
    app["environment"] += [
        {"name": name, "value": value}
        for name, value in python_environment(request, user_path).items()
        if name not in present
    ]
    payload.setdefault("volumes", []).append({"name": PYTHON_VOLUME})
    app.setdefault("mountPoints", []).append(
        {"sourceVolume": PYTHON_VOLUME, "containerPath": PYTHON_PATH, "readOnly": True}
    )
    app.setdefault("dependsOn", []).append({"containerName": PYTHON_INIT, "condition": "SUCCESS"})
    payload["containerDefinitions"].append(
        {
            "name": PYTHON_INIT,
            "image": env.python_autoinstrumentation_image,
            "essential": False,
            "memoryReservation": 32,
            "mountPoints": [{"sourceVolume": PYTHON_VOLUME, "containerPath": PYTHON_PATH}],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": dict(app["logConfiguration"]["options"]),
            },
        }
    )


def add_tracing(
    payload: dict, env: OneportEcsEnvironment, release: str, request: DeploymentRequest
) -> None:
    """Adds the sidecar and OTEL_* variables. Values the app already sets win."""
    if env.tracing != "xray":
        return
    app = payload["containerDefinitions"][0]
    if request.tracing_enabled is False:
        app["environment"] = [i for i in app["environment"] if i["name"] != "OTEL_SDK_DISABLED"]
        app["environment"].append({"name": "OTEL_SDK_DISABLED", "value": "true"})
        return
    kind = language(request)
    if kind == "python":
        _add_python(payload, env, request)
    elif kind in IMAGES:
        values = {i["name"]: i["value"] for i in app["environment"]}
        updates = agent_environment(kind, values)
        app["environment"] = [i for i in app["environment"] if i["name"] not in updates]
        app["environment"] += [{"name": k, "value": v} for k, v in updates.items()]
        payload.setdefault("volumes", []).append({"name": "otel-agent"})
        app.setdefault("mountPoints", []).append(
            {"sourceVolume": "otel-agent", "containerPath": PATH, "readOnly": True}
        )
        app.setdefault("dependsOn", []).append(
            {"containerName": "otel-agent-init", "condition": "SUCCESS"}
        )
        payload["containerDefinitions"].append(
            {
                "name": "otel-agent-init",
                "image": IMAGES[kind],
                "essential": False,
                "entryPoint": ["/bin/sh", "-ec"],
                "command": [copy_command(kind)],
                "mountPoints": [{"sourceVolume": "otel-agent", "containerPath": PATH}],
                "logConfiguration": app["logConfiguration"],
            }
        )
    base = app_environment(env, release)
    if kind not in {None, "python"}:
        base["OTEL_PROPAGATORS"] = "tracecontext,baggage"
    present = {item["name"] for item in app["environment"]}
    app["environment"] += [
        {"name": name, "value": value} for name, value in base.items() if name not in present
    ]
    payload["containerDefinitions"].append(sidecar(env, app["logConfiguration"]["options"]))
