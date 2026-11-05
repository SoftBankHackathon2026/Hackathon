import threading

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import AuthorizedSession

from controlplane.domain.models import Runtime
from controlplane.execution.gcp.auth import credentials
from controlplane.execution.gcp.cache import cache_status
from controlplane.execution.gcp.data import data_status
from controlplane.execution.gcp.states import available_actions
from controlplane.providers.contracts import ProviderUnavailable
from controlplane.providers.durable import DurableDeploymentProvider
from controlplane.providers.gcp.capability_spec import build_capabilities
from controlplane.providers.gcp.models import GcpDeploymentRequest
from controlplane.providers.gcp.plan import plan_steps
from controlplane.providers.gcp.telemetry import GcpTelemetry


class GcpAdapter(DurableDeploymentProvider):
    runtime = Runtime.GCP_CLOUD_RUN
    supports_restore = True
    service_access = "authenticated"

    def __init__(self, settings, *, store=None):
        super().__init__(settings, store=store)
        self.service_access = settings.service_access
        self.application = settings.environment.application
        self.environment_id = settings.environment.environment_id
        self._monitoring_session = None
        self._monitoring_lock = threading.Lock()

    def _telemetry(self):
        """One authorized session for every monitoring read: the access token is
        fetched once and refreshed when it expires, not on every request."""
        with self._monitoring_lock:
            if self._monitoring_session is None:
                try:
                    self._monitoring_session = AuthorizedSession(credentials(self.settings))
                except GoogleAuthError as exc:
                    raise ProviderUnavailable("GCP monitoring authentication failed") from exc
        return GcpTelemetry(self.settings, session=self._monitoring_session)

    def _read(self, read):
        try:
            with self._telemetry() as reader:
                return read(reader)
        except ProviderUnavailable:
            # Credentials may have been rotated; load them again on the next read.
            # The session is not closed: another thread may still be using it.
            with self._monitoring_lock:
                self._monitoring_session = None
            raise

    def app_logs(self, *, minutes, filter_text, limit):
        return self._read(
            lambda reader: reader.app_logs(minutes=minutes, filter_text=filter_text, limit=limit)
        )

    def traffic(self, *, minutes):
        return self._read(lambda reader: reader.traffic(minutes=minutes))

    def traces(self, *, minutes, kind):
        return self._read(lambda reader: reader.traces(minutes=minutes, kind=kind))

    def trace(self, trace_id):
        return self._read(lambda reader: reader.trace(trace_id))

    def capabilities(self):
        return build_capabilities(self.runtime, self.settings)

    def parse_request(self, payload):
        payload = dict(payload)
        if payload.get("rollout") is None:
            payload.pop("rollout", None)
        request = GcpDeploymentRequest.model_validate(payload)
        self.settings.validate_request(request)
        if (
            self.settings.data_provisioning
            and request.resources.database == "none"
            and data_status(self.store, self.settings)
        ):
            raise ValueError(
                "이 앱은 PostgreSQL 준비를 시작했습니다. 같은 DB를 선택해 재사용하세요."
            )
        if (
            self.settings.cache_provisioning
            and not request.resources.redis
            and cache_status(self.store, self.settings) is not None
        ):
            raise ValueError("이 앱은 캐시 준비를 시작했습니다. 같은 Redis를 선택해 재사용하세요.")
        return request

    def fingerprint(self):
        return self.settings.fingerprint()

    def deletion_info(self):
        env = self.settings.environment
        keeps = ["Cloud Logging 로그"]
        if self.settings.cache_provisioning and cache_status(self.store, self.settings) is not None:
            keeps.append("Redis 캐시·연결 URL·TLS CA Secret (별도 삭제 필요, 사용료 계속 발생)")
        data = (
            [f"Cloud SQL oneport-db-{self.settings.data_provisioning.app_id}과 DB 연결 Secret"]
            if self.settings.data_provisioning and data_status(self.store, self.settings)
            else []
        )
        return {
            "supported": True,
            "removes": [f"Cloud Run 서비스 {env.service}와 모든 리비전"],
            "data": data,
            "keeps": keeps,
            "recreatable": False,
            "after": "이 환경은 미리 등록된 Cloud Run 서비스를 갱신하는 방식입니다. 삭제한 뒤에는 운영자가 서비스를 다시 준비해야 배포할 수 있습니다.",
        }

    def plan_steps(self, request):
        return plan_steps(request, first_deployment=self._first_deployment())

    def _first_deployment(self) -> bool:
        if not self.settings.catalog_dynamic:
            return False
        with self.store.connect() as conn:
            app = conn.execute(
                "SELECT checkpoint FROM gcp_apps WHERE service_name=%s",
                (self.settings.environment.service_name,),
            ).fetchone()
        return not app or not app["checkpoint"].get("initialized")

    def snapshot(self, row):
        return super().snapshot(row).model_copy(update={"actions": available_actions(row)})

    def request_action(self, operation, action):
        self.observe(operation)
        row = self.store.request_action(
            operation.id,
            action,
            application=self.application,
            environment_id=self.environment_id,
            binding_fingerprint=self.fingerprint(),
        )
        return self.snapshot(row)

    def public_environment(self):
        env = self.settings.environment
        data = data_status(self.store, self.settings) if self.settings.data_provisioning else None
        cache = (
            cache_status(self.store, self.settings) if self.settings.cache_provisioning else None
        )
        return {
            "id": self.environment_id,
            "name": f"{self.application} · GCP Cloud Run",
            "application": self.application,
            "runtime": self.runtime,
            "release_restore": self.supports_restore,
            "state": "registered",
            "executable": True,
            "connection_verified": False,
            "images": self.settings.allowed_images,
            "max_replicas": env.max_replicas,
            "max_cpu": env.max_cpu,
            "max_memory_mib": env.max_memory_mib,
            "service_url": env.service_url,
            "service_access": self.service_access,
            "input_kind": "image",
            "configurable_rollout": True,
            "monitoring_enabled": True,
            "tracing": "cloud_trace",
            "instrumentation_options": self._instrumentation_options(),
            "data_provisioning": _provisioning_view(
                self.settings.data_provisioning, data, "instance"
            ),
            "cache_provisioning": _provisioning_view(
                self.settings.cache_provisioning, cache, "new"
            ),
            "request_template": self._request_template(data, cache),
        }

    def _instrumentation_options(self):
        tracing = self.settings.tracing
        if not tracing:
            return []
        return (
            ["auto", "sdk"]
            + (["python"] if tracing.python_image else [])
            + list(tracing.agent_images)
        )

    def _request_template(self, data, cache):
        return GcpDeploymentRequest(
            application=self.application,
            environment_id=self.environment_id,
            source={"kind": "image", "image": self.settings.allowed_images[0]},
            cpu="1",
            memory_mib=512,
            resources={
                "database": "postgresql" if data else "none",
                "database_mode": "dedicated",
                "redis": cache is not None,
            },
        ).model_dump(mode="json")


def _provisioning_view(configured_settings, checkpoint, default_phase):
    return {
        "enabled": configured_settings is not None,
        "configured": checkpoint is not None,
        "state": checkpoint.get("phase", default_phase) if checkpoint else "none",
    }
