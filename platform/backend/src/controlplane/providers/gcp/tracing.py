"""Cloud Run collector and optional zero-code Python instrumentation.

Only named Oneport sidecars are managed. Startup probes gate shared-volume
initialization; the helper stays alive because Cloud Run has no init containers.
"""

import json
import re

from google.cloud import run_v2

from controlplane.domain.instrumentation import PATH as AGENT_PATH
from controlplane.domain.instrumentation import (
    agent_environment,
    clean_environment,
    copy_command,
    language,
)
from controlplane.providers.contracts import ProviderConflict

COLLECTOR_IMAGE = (
    "docker.io/otel/opentelemetry-collector-contrib:0.162.0-amd64"
    "@sha256:340885ab6f46822374f00c58c067396b53b8d612559842e8821585567579d9e3"
)
PYTHON_IMAGE = (
    "ghcr.io/open-telemetry/opentelemetry-operator/autoinstrumentation-python:0.66b0"
    "@sha256:0baba8785795d1ecaf9e5aafba9b400bae59ea4dba03f320d59845abf63d623e"
)
COLLECTOR = "oneport-otel"
PYTHON = "oneport-otel-python"
VOLUME = "oneport-otel-python"
PATH = "/otel-auto-instrumentation-python"
AGENT = "oneport-otel-agent"
MANAGED = {COLLECTOR, PYTHON, AGENT}
RESERVED_PORTS = {4317, 4318, 13133, 13134}


def collector_config(env, release):
    # JSON is valid YAML. Fixed span identity prevents a caller-supplied service.name
    # from accidentally mixing this app with another registered environment.
    statements = [
        f'set(attributes["{key}"], {json.dumps(value)})'
        for key, value in {
            "oneport.application": env.application,
            "oneport.environment": env.environment_id,
            "oneport.service": env.service,
            "oneport.release": release,
        }.items()
    ]
    statements += ['set(attributes["oneport.error"], true) where status.code == STATUS_CODE_ERROR']
    return json.dumps(
        {
            "receivers": {
                "otlp": {
                    "protocols": {
                        "http": {"endpoint": "127.0.0.1:4318"},
                        "grpc": {"endpoint": "127.0.0.1:4317"},
                    }
                }
            },
            "processors": {
                "memory_limiter": {"check_interval": "1s", "limit_mib": 96, "spike_limit_mib": 32},
                "transform/identity": {
                    "error_mode": "ignore",
                    "trace_statements": [
                        {"context": "span", "statements": statements},
                        {
                            "context": "spanevent",
                            "statements": [
                                'set(span.attributes["exception.message"], attributes["exception.message"]) where name == "exception"',
                            ],
                        },
                    ],
                },
                "batch": {"timeout": "1s", "send_batch_size": 100, "send_batch_max_size": 200},
            },
            "exporters": {"googlecloud": {"project": env.project_id}},
            "extensions": {"health_check": {"endpoint": "0.0.0.0:13133"}},
            "service": {
                "extensions": ["health_check"],
                "pipelines": {
                    "traces": {
                        "receivers": ["otlp"],
                        "processors": ["memory_limiter", "transform/identity", "batch"],
                        "exporters": ["googlecloud"],
                    }
                },
            },
        }
    )


def prepare_app(template):
    apps = [c for c in template.containers if c.name not in MANAGED]
    if len(apps) != 1 or template.containers[0].name in MANAGED:
        raise ProviderConflict("Only one app and Oneport tracing sidecars are supported")
    return template.containers[0]


def _startup_probe(path, port):
    return run_v2.Probe(
        http_get=run_v2.HTTPGetAction(path=path, port=port),
        period_seconds=1,
        timeout_seconds=1,
        failure_threshold=60,
    )


def _memory_volume(name, size):
    return run_v2.Volume(
        name=name, empty_dir=run_v2.EmptyDirVolumeSource(medium="MEMORY", size_limit=size)
    )


def _helper_container(name, volume, image, copy, mount_path, memory):
    return run_v2.Container(
        name=name,
        image=image,
        command=["/bin/sh"],
        args=[
            "-ec",
            f"{copy}; mkdir -p /tmp/ready; touch /tmp/ready/ready; "
            "exec busybox httpd -f -p 13134 -h /tmp/ready",
        ],
        volume_mounts=[run_v2.VolumeMount(name=volume, mount_path=mount_path)],
        resources=run_v2.ResourceRequirements(limits={"cpu": "1", "memory": memory}),
        startup_probe=_startup_probe("/ready", 13134),
    )


def _otel_environment(request, env, release, settings):
    return {
        "OTEL_SERVICE_NAME": env.application,
        "OTEL_RESOURCE_ATTRIBUTES": f"service.version={release},deployment.environment.name={env.environment_id}",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_PROPAGATORS": "tracecontext,baggage",
        "OTEL_TRACES_SAMPLER": "traceidratio",
        "OTEL_TRACES_SAMPLER_ARG": str(settings.sample_ratio),
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_SDK_DISABLED": "false",
        "OTEL_PYTHON_LOG_CORRELATION": "true",
        "OTEL_PYTHON_LOG_FORMAT": "%(asctime)s %(levelname)s [trace_id=%(otelTraceID)s span_id=%(otelSpanID)s] %(message)s",
        "OTEL_PYTHON_EXCLUDED_URLS": re.escape(request.health_path) + "$",
    }


def _reset_app(template, app):
    template.containers = [app]
    app = template.containers[0]
    app.name = app.name or "app"
    app.depends_on = [n for n in app.depends_on if n not in MANAGED]
    template.volumes = [v for v in template.volumes if v.name not in {VOLUME, AGENT}]
    app.volume_mounts = [m for m in app.volume_mounts if m.name not in {VOLUME, AGENT}]
    return app


def _user_pythonpath(values):
    return ":".join(
        p for p in values.get("PYTHONPATH", "").split(":") if p and not p.startswith(PATH)
    )


def _disable_tracing(app, values, cleaned, restore_path):
    changes = {k: v for k, v in cleaned.items() if values.get(k) != v}
    if restore_path is not None:
        changes["PYTHONPATH"] = restore_path
    changes["OTEL_SDK_DISABLED"] = "true"
    app.env = [e for e in app.env if e.name not in changes and not e.name.startswith("OTEL_")] + [
        run_v2.EnvVar(name=k, value=v) for k, v in changes.items()
    ]


def _add_python_sidecar(template, app, settings):
    template.volumes.append(_memory_volume(VOLUME, "96Mi"))
    app.volume_mounts.append(run_v2.VolumeMount(name=VOLUME, mount_path=PATH))
    app.depends_on.append(PYTHON)
    source = (
        "/autoinstrumentation-musl"
        if settings.python_platform == "musl"
        else "/autoinstrumentation"
    )
    template.containers.append(
        _helper_container(
            PYTHON, VOLUME, settings.python_image, f"cp -r {source}/. {PATH}/", PATH, "128Mi"
        )
    )


def _add_agent_sidecar(template, app, kind, image):
    template.volumes.append(_memory_volume(AGENT, "512Mi"))
    app.volume_mounts.append(run_v2.VolumeMount(name=AGENT, mount_path=AGENT_PATH))
    app.depends_on.append(AGENT)
    template.containers.append(
        _helper_container(AGENT, AGENT, image, copy_command(kind), AGENT_PATH, "512Mi")
    )


def _collector_container(env, release, settings):
    return run_v2.Container(
        name=COLLECTOR,
        image=settings.collector_image,
        args=["--config=env:ONEPORT_OTEL_CONFIG"],
        env=[run_v2.EnvVar(name="ONEPORT_OTEL_CONFIG", value=collector_config(env, release))],
        resources=run_v2.ResourceRequirements(limits={"cpu": "1", "memory": "256Mi"}),
        startup_probe=_startup_probe("/", 13133),
    )


def configure(template, request, env, release, settings):
    app = prepare_app(template)
    existing = {c.name for c in template.containers} & MANAGED
    if settings is None:
        if existing:
            raise ProviderConflict("Keep the registered tracing configuration for this service")
        return
    if request.port in RESERVED_PORTS:
        raise ProviderConflict("The application port conflicts with a tracing sidecar")
    app = _reset_app(template, app)
    values = {e.name: e.value for e in app.env}
    cleaned = clean_environment(values)
    app.env = [e for e in app.env if e.name in cleaned]
    kind = language(request)
    path = _user_pythonpath(values)
    managed_path = bool(existing) and "PYTHONPATH" in values
    if request.tracing_enabled is False:
        _disable_tracing(app, values, cleaned, path if managed_path else None)
        return
    updates = _otel_environment(request, env, release, settings)
    updates.update({k: v for k, v in cleaned.items() if values.get(k) != v})
    if kind == "python":
        updates["PYTHONPATH"] = (
            f"{PATH}/opentelemetry/instrumentation/auto_instrumentation:{PATH}"
            + (":" + path if path else "")
        )
        _add_python_sidecar(template, app, settings)
    elif managed_path:
        updates["PYTHONPATH"] = path
    if kind in {"java", "nodejs", "dotnet"}:
        image = settings.agent_images.get(kind)
        if image is None:
            raise ProviderConflict("Detected language has no registered Cloud Run agent image")
        updates.update(agent_environment(kind, cleaned, platform=settings.python_platform))
        _add_agent_sidecar(template, app, kind, image)
    app.env = [e for e in app.env if e.name not in updates] + [
        run_v2.EnvVar(name=k, value=v) for k, v in sorted(updates.items())
    ]
    app.depends_on.append(COLLECTOR)
    template.containers.append(_collector_container(env, release, settings))
    for c in template.containers:
        c.resources.cpu_idle = False
