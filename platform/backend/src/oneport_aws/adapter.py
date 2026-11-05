"""API binding around the team's AWS runtime; HTTP intake never calls AWS.

Only the operator monitoring views (logs, traffic, traces) read AWS from the API
process, read-only.
"""

import threading
from typing import Any

from controlplane.core.errors import RequestConflict
from controlplane.domain.models import DeploymentRequest, Runtime
from controlplane.providers.capabilities import provider_capabilities
from controlplane.providers.contracts import ProviderUnavailable
from controlplane.providers.durable import DurableDeploymentProvider
from controlplane.providers.interface import AppLogs
from controlplane.providers.telemetry import TraceDetail, TraceKind, TraceList, Traffic
from oneport_aws import adapter_view
from oneport_aws.execution.oneport_ecs_apps import AppRegistry
from oneport_aws.providers.oneport_ecs import logs, telemetry, traces
from oneport_aws.providers.oneport_ecs.connection import connect
from oneport_aws.providers.oneport_ecs.models import credentials_from_env_file
from oneport_aws.providers.oneport_ecs.provider import OneportEcsProvider

MAX_CACHE_APP_ID_LENGTH = 24


class AwsAdapter(DurableDeploymentProvider):
    runtime = Runtime.AWS_ECS
    supports_restore = True

    def __init__(self, settings, *, store=None, clients=None):
        super().__init__(settings, store=store)
        self.application = settings.application
        self.environment_id = settings.environment_id
        self.apps = AppRegistry(settings.database_url.get_secret_value())
        self.environment = settings.environment()
        self._clients = clients
        self._log_reader: OneportEcsProvider | None = None
        self._log_lock = threading.Lock()

    def initialize(self):
        self.apps.migrate()

    def _reader(self) -> OneportEcsProvider:
        """Connects on first use, with the same credentials as the worker."""
        with self._log_lock:
            if self._log_reader is None:
                try:
                    clients = self._clients or connect(
                        credentials_from_env_file(self.settings.env_file),
                        self.environment.region,
                    )
                except (OSError, KeyError, ValueError):
                    raise ProviderUnavailable("AWS 접속 설정을 읽지 못했습니다.") from None
                self._log_reader = OneportEcsProvider(clients, self.environment)
            return self._log_reader

    def app_logs(self, *, minutes: int, filter_text: str | None, limit: int) -> AppLogs:
        lines, truncated = logs.latest(
            self._reader(), minutes=minutes, filter_text=filter_text, limit=limit
        )
        return AppLogs(
            application=self.application,
            environment_id=self.environment_id,
            minutes=minutes,
            lines=lines,
            truncated=truncated,
        )

    def traffic(self, *, minutes: int) -> Traffic:
        return telemetry.traffic(self._reader(), minutes=minutes)

    def traces(self, *, minutes: int, kind: TraceKind) -> TraceList:
        enabled = self.environment.tracing == "xray"
        found = traces.traces(self._reader(), minutes=minutes, kind=kind) if enabled else []
        return TraceList(
            application=self.application,
            environment_id=self.environment_id,
            minutes=minutes,
            kind=kind,
            tracing_enabled=enabled,
            traces=found,
        )

    def trace(self, trace_id: str) -> TraceDetail | None:
        return traces.trace(self._reader(), trace_id)

    def capabilities(self):
        return provider_capabilities(self.runtime)

    def fingerprint(self) -> str:
        return self.settings.fingerprint()

    def parse_request(self, payload: Any) -> DeploymentRequest:
        request = DeploymentRequest.model_validate(payload)
        app = self.apps.get(self.application, self.environment_id)
        if app is None:
            raise RequestConflict("AWS 앱 등록을 먼저 완료하세요.")
        adapter_view.check_request_matches(app, request)
        waiting = adapter_view.waiting_message(app)
        if waiting:
            raise RequestConflict(waiting)
        self.settings.validate_request(
            request, extra_refs=self.apps.refs(self.application, self.environment_id)
        )
        return request

    def restore(self, operation, operation_key, *, team=None, created_by=None):
        """The worker reuses the earlier release's task definition revision (same
        image, env and secrets) when it finds this mapping at "preparing"."""
        restored = super().restore(operation, operation_key, team=team, created_by=created_by)
        self.apps.set_restore(restored.id, operation.id)
        return restored

    def register_application(
        self, *, port, health_path, database="none", database_mode="shared", redis=False
    ):
        if redis and len(self.application) > MAX_CACHE_APP_ID_LENGTH:
            raise ValueError("캐시를 쓰는 앱 ID는 24자 이하여야 합니다 (ElastiCache 이름 제한).")
        shared_ref = self.settings.secret_refs.get("db") if database_mode == "shared" else None
        self.apps.register(
            self.environment,
            port=port,
            health_path=health_path,
            database=database,
            database_mode=database_mode,
            shared_ref=shared_ref,
            redis=redis,
        )
        return self.public_environment()

    def deletion_info(self) -> dict[str, Any]:
        app = self.apps.get(self.application, self.environment_id)
        return adapter_view.deletion_info(self.environment, app)

    def public_environment(self) -> dict[str, Any]:
        app = self.apps.get(self.application, self.environment_id)
        return adapter_view.public_environment(self.settings, self.environment, app)

    def plan_steps(self, request: DeploymentRequest):
        return adapter_view.plan_steps(request)
