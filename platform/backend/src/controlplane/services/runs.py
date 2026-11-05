from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from controlplane.builds.models import BuildSettings
from controlplane.core.errors import (
    InvalidRequest,
    NotConfigured,
    OperationNotFound,
    RequestConflict,
)
from controlplane.demo_deployments.models import (
    DemoDeployInput,
    PreviewInput,
    RetryRun,
    RunSettings,
    StartRun,
)
from controlplane.demo_deployments.service import RunService, public_run
from controlplane.schemas.accounts import Identity
from controlplane.services.deployments import DeploymentService

Row = dict[str, Any]


@contextmanager
def _run_errors() -> Iterator[None]:
    try:
        yield
    except (OperationNotFound, RequestConflict):
        raise
    except ValueError:
        raise InvalidRequest("배포 입력 또는 대상 설정을 확인하세요.") from None


class DemoRunService:
    def __init__(
        self,
        settings: RunSettings | None,
        build_settings: BuildSettings | None,
        deployments: DeploymentService,
    ) -> None:
        self._runs = RunService(settings, build_settings, deployments)
        self._ready = bool(settings and build_settings and self._runs.store)

    def _call(self, action: Callable[[], Row]) -> Row:
        if not self._ready:
            raise NotConfigured("다중 환경 배포 Worker와 기본 대상 설정이 필요합니다.")
        with _run_errors():
            return action()

    def preview(self, request: PreviewInput, identity: Identity | None) -> Row:
        return self._call(lambda: self._runs.preview(request, identity))

    def launch(self, request: DemoDeployInput, identity: Identity | None) -> Row:
        return self._call(lambda: public_run(self._runs.launch(request, identity)))

    def start(self, request: StartRun, identity: Identity | None) -> Row:
        return self._call(lambda: public_run(self._runs.start(request, identity)))

    def get(self, run_id: str, identity: Identity | None) -> Row:
        return self._call(lambda: public_run(self._runs.store.get(run_id, identity)))

    def retry(self, run_id: str, request: RetryRun, identity: Identity | None) -> Row:
        return self._call(lambda: public_run(self._runs.retry(run_id, request, identity)))
