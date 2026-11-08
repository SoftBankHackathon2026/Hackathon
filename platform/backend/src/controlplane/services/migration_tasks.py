import threading
from collections.abc import Callable
from itertools import count

from controlplane.core.errors import InvalidRequest, OperationNotFound, RequestConflict
from controlplane.domain.fleet import FleetConfig, MigrationConfig
from controlplane.domain.migration import Direction
from controlplane.domain.task_plan import ONPREM, route_label
from controlplane.repositories.migration_tasks import TaskRepository
from controlplane.schemas.migrations import MigrationCreate, MigrationCreated

SAME_ENVIRONMENT = "출발 환경과 도착 환경이 같아요. 서로 다른 환경을 골라 주세요."
PROVIDER_REQUIRED = "플랫폼이 모르는 환경이에요. 환경 종류(온프렘·AWS·GCP)를 골라 주세요."
TASK_NOT_FOUND = "등록된 마이그레이션 작업이 없어요."
FIXED_TASK = "설정 파일(fleet.json)에 적힌 작업은 화면에서 지울 수 없어요."
TASK_STARTED = "전환을 시작했거나 실행 중인 명령이 있는 작업은 지울 수 없어요."


class MigrationTaskService:
    def __init__(self, config: FleetConfig, repository: TaskRepository) -> None:
        self._config = config
        self._repository = repository
        self._lock = threading.Lock()
        self._fixed = list(config.migrations)
        self._fixed_ids = {task.job_id for task in self._fixed}

    def sync(self) -> None:
        stored = [
            self._with_defaults(task)
            for task in self._repository.list()
            if task.job_id not in self._fixed_ids
        ]
        self._config.migrations[:] = [*self._fixed, *stored]

    def removable(self, job_id: str, started: Callable[[str], bool]) -> bool:
        if job_id in self._fixed_ids or self._config.migration(job_id) is None:
            return False
        return not started(job_id)

    def remove(self, job_id: str, started: Callable[[str], bool]) -> None:
        with self._lock:
            if self._config.migration(job_id) is None:
                raise OperationNotFound(TASK_NOT_FOUND)
            if job_id in self._fixed_ids:
                raise InvalidRequest(FIXED_TASK)
            if started(job_id):
                raise RequestConflict(TASK_STARTED)
            self._repository.remove(job_id)
            self.sync()

    def _with_defaults(self, task: MigrationConfig) -> MigrationConfig:
        defaults = self._config.task_defaults
        update = {
            name: value
            for name, value in (
                ("worker_url", defaults.worker_url),
                ("connect_url", defaults.connect_url),
            )
            if value and not getattr(task, name)
        }
        return task.model_copy(update=update) if update else task

    def _provider(self, env_id: str, given: str | None) -> str:
        try:
            return self._config.environment(env_id).provider
        except KeyError:
            if given is None:
                raise InvalidRequest(PROVIDER_REQUIRED) from None
            return given

    def create(self, body: MigrationCreate, created_by: str | None) -> MigrationCreated:
        if body.source_env == body.target_env:
            raise InvalidRequest(SAME_ENVIRONMENT)
        source = self._provider(body.source_env, body.source_provider)
        target = self._provider(body.target_env, body.target_provider)
        with self._lock:
            self.sync()
            used = {task.job_id for task in self._config.migrations}
            prefix = f"{source}_to_{target}"
            job_id = next(f"{prefix}_{n:03d}" for n in count(1) if f"{prefix}_{n:03d}" not in used)
            task = MigrationConfig(
                job_id=job_id,
                direction=Direction.RETURN if target == ONPREM else Direction.FORWARD,
                source_env=body.source_env,
                target_env=body.target_env,
                data_source=body.data_source or body.source_env,
                data_target=body.data_target or body.target_env,
                source_provider=source,
                target_provider=target,
                apps=body.apps,
            )
            self._repository.add(task, created_by)
            self._config.migrations.append(self._with_defaults(task))
        return MigrationCreated(job_id=job_id, label=route_label(source, target))
