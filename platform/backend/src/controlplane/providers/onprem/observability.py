"""Operator-owned telemetry endpoints and opt-in Python instrumentation.

Queries use the existing TLS-verified Kubernetes API and narrowly scoped service
proxy RBAC. Applications never receive the platform kubeconfig or query access.
"""

import json
from enum import StrEnum
from typing import Literal

from kubernetes.client.exceptions import ApiException
from pydantic import Field

from controlplane.domain.instrumentation import IMAGES, agent_environment, language
from controlplane.domain.models import Model
from controlplane.providers.contracts import ProviderUnavailable


class ObservabilitySettings(Model):
    namespace: Literal["oneport-observability"] = "oneport-observability"
    prometheus_service: Literal["prometheus:9090"] = "prometheus:9090"
    tempo_service: Literal["tempo:3200"] = "tempo:3200"
    # Omit the legacy default so existing deployment binding hashes remain valid.
    logs_backend: Literal["pods", "loki"] = Field(
        default="pods", exclude_if=lambda value: value == "pods"
    )
    python_image: str | None = Field(default=None, pattern=r"^[^\s]+@sha256:[a-f0-9]{64}$")
    python_platform: Literal["glibc", "musl"] = "glibc"
    trace_sample_ratio: float = Field(default=0.25, gt=0, le=1)

    @property
    def endpoint(self):
        return f"http://otel-collector.{self.namespace}.svc.cluster.local:4318"


def instrumentation_values(settings, request, release):
    """Identity/export settings are platform-owned, never public request overrides."""
    config = settings.observability
    if request.instrumentation is None or request.tracing_enabled is False:
        return None
    kind = (
        ("sdk" if request.instrumentation == "auto" else request.instrumentation)
        if release == "validation"
        else language(request)
    )
    if config is None or (kind == "python" and config.python_image is None):
        raise ValueError("Python instrumentation is not configured for this environment")
    if any(key.startswith("OTEL_") for key in request.env):
        raise ValueError("OTEL_* variables are managed by the platform when instrumentation is on")
    if kind != "python":
        return {
            "python": False,
            "language": kind,
            "image": IMAGES.get(kind, ""),
            "platform": config.python_platform,
            "endpoint": config.endpoint,
            "sampleRatio": config.trace_sample_ratio,
            "agentEnv": agent_environment(kind, request.env, platform=config.python_platform),
        }
    return {
        "python": True,
        "image": config.python_image,
        "platform": config.python_platform,
        "endpoint": config.endpoint,
        "sampleRatio": config.trace_sample_ratio,
    }


class TelemetryBackend(StrEnum):
    PROMETHEUS = "prometheus"
    TEMPO = "tempo"
    LOKI = "loki"


LOKI_SERVICE = "loki:3100"
LOKI_QUERY_PATH = "loki/api/v1/query_range"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
PROXY_PATH = "/api/v1/namespaces/{namespace}/services/{name}/proxy/{path}"


class TelemetryProxy:
    def __init__(self, clients, settings):
        self.clients, self.settings = clients, settings

    def _service_name(self, backend: TelemetryBackend, path: str) -> str:
        if backend == TelemetryBackend.LOKI and path == LOKI_QUERY_PATH:
            return LOKI_SERVICE
        if backend in {TelemetryBackend.PROMETHEUS, TelemetryBackend.TEMPO} and path.startswith(
            "api/"
        ):
            return getattr(self.settings, backend + "_service")
        raise ValueError("Unregistered telemetry endpoint")

    def _fetch(self, name: str, path: str, params: dict | None) -> bytes:
        response = self.clients.api.call_api(
            PROXY_PATH,
            "GET",
            path_params={"namespace": self.settings.namespace, "name": name, "path": path},
            query_params=list((params or {}).items()),
            header_params={"Accept": "application/json"},
            auth_settings=["BearerToken"],
            _return_http_data_only=True,
            _preload_content=False,
            _request_timeout=(3, 15),
        )
        try:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            response.close()
            response.release_conn()
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("Telemetry response too large")
        return body

    def get(self, backend: TelemetryBackend, path: str, params: dict | None = None):
        name = self._service_name(backend, path)
        try:
            return json.loads(self._fetch(name, path, params))
        except ApiException as exc:
            if (
                exc.status == 404
                and backend == TelemetryBackend.TEMPO
                and path.startswith("api/traces/")
            ):
                return None
            raise ProviderUnavailable("온프렘 관측 저장소에 연결하지 못했습니다.") from None
        except Exception:
            raise ProviderUnavailable("온프렘 관측 저장소에 연결하지 못했습니다.") from None
