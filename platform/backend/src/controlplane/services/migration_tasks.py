import threading
from itertools import count

from controlplane.core.errors import InvalidRequest
from controlplane.domain.fleet import FleetConfig, MigrationConfig
from controlplane.domain.migration import Direction
from controlplane.domain.task_plan import ONPREM, route_label
from controlplane.repositories.migration_tasks import TaskRepository
from controlplane.schemas.migrations import MigrationCreate, MigrationCreated

SAME_ENVIRONMENT = "출발 환경과 도착 환경이 같아요. 서로 다른 환경을 골라 주세요."
PROVIDER_REQUIRED = "플랫폼이 모르는 환경이에요. 환경 종류(온프렘·AWS·GCP)를 골라 주세요."


class MigrationTaskService:
    def __init__(self, config: FleetConfig, repository: TaskRepository) -> None:
        self._config = config
        self._repository = repository
        self._lock = threading.Lock()

    def sync(self) -> None:
        known = {task.job_id for task in self._config.migrations}
        for task in self._repository.list():
            if task.job_id not in known:
                self._config.migrations.append(task)
                known.add(task.job_id)

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
            self._config.migrations.append(task)
        return MigrationCreated(job_id=job_id, label=route_label(source, target))
