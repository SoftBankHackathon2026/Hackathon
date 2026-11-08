import threading
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import count

from controlplane.core.errors import InvalidRequest
from controlplane.domain.fleet import FleetConfig, MigrationConfig
from controlplane.domain.migration import Direction
from controlplane.domain.task_plan import ONPREM, route_label
from controlplane.repositories.migration_tasks import TaskRepository
from controlplane.schemas.accounts import Identity
from controlplane.schemas.migrations import (
    MigrationCreate,
    MigrationCreated,
    MigrationSummary,
    SkippedTask,
    TaskDeletion,
    TaskDeletionResult,
)

SAME_ENVIRONMENT = "출발 환경과 도착 환경이 같아요. 서로 다른 환경을 골라 주세요."
PROVIDER_REQUIRED = "플랫폼이 모르는 환경이에요. 환경 종류(온프렘·AWS·GCP)를 골라 주세요."
SCREEN_REQUESTER = "운영 화면"
TASK_ACTIVE = "진행 중인 작업"
TASK_FIXED = "설정 파일 작업"
TASK_MISSING = "없는 작업"

# 작업 ID 묶음을 받아 그중 진행 중인 것(전환 단계, CDC 커넥터, 대기/실행 중 명령)을 돌려준다.
BusyJobs = Callable[[set[str]], set[str]]


class MigrationTaskService:
    """화면이나 생성 API 로 등록한 작업을 플랫폼 DB 에서 읽어 설정의 작업 목록에 더하고 뺀다.

    설정 파일(fleet.json)에 적힌 작업은 건드리지 않는다. 등록한 작업만 지울 수 있고, 지운 작업은
    sync 에서 목록에서 빠지므로 대시보드, 수집기, 보고 수신, 러너 명령 배정이 모두 더는 보지 않는다.
    """

    def __init__(
        self,
        config: FleetConfig,
        repository: TaskRepository,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._repository = repository
        self._now = now
        self._lock = threading.Lock()
        self._registered: set[str] = set()

    def sync(self) -> None:
        with self._lock:
            self._sync()

    def _sync(self) -> None:
        stored = self._repository.list()
        visible = {task.job_id for task in stored}
        gone = self._registered - visible
        if gone:
            self._config.migrations[:] = [
                task for task in self._config.migrations if task.job_id not in gone
            ]
            self._registered -= gone
        known = {task.job_id for task in self._config.migrations}
        for task in stored:
            if task.job_id not in known:
                self._config.migrations.append(self._with_defaults(task))
                self._registered.add(task.job_id)
                known.add(task.job_id)

    def registered(self, job_id: str) -> bool:
        return job_id in self._registered

    def mark(self, summaries: list[MigrationSummary], permitted: bool, busy_jobs: BusyJobs) -> None:
        """등록한 작업인지, 지금 지울 수 있는지 표시한다. 설정 파일 작업은 늘 지울 수 없다."""
        registered = {s.job_id for s in summaries if self.registered(s.job_id)}
        busy = busy_jobs(registered) if permitted and registered else set()
        for summary in summaries:
            summary.registered = summary.job_id in registered
            summary.can_delete = permitted and summary.registered and summary.job_id not in busy

    def delete(
        self, deletion: TaskDeletion, identity: Identity | None, busy_jobs: BusyJobs
    ) -> TaskDeletionResult:
        hidden_by = identity.email if identity else SCREEN_REQUESTER
        reasons: dict[str, str] = {}
        with self._lock:
            self._sync()
            for job_id in deletion.job_ids:
                if self._config.migration(job_id) is None:
                    reasons[job_id] = TASK_MISSING
                elif not self.registered(job_id):
                    reasons[job_id] = TASK_FIXED
            candidates = {job_id for job_id in deletion.job_ids if job_id not in reasons}
            for job_id in busy_jobs(candidates) if candidates else set():
                reasons[job_id] = TASK_ACTIVE
            asked = [job_id for job_id in deletion.job_ids if job_id not in reasons]
            hidden, kept = (
                self._repository.hide(asked, hidden_by, self._now()) if asked else (set(), set())
            )
            for job_id in asked:
                if job_id not in hidden:
                    reasons[job_id] = TASK_ACTIVE if job_id in kept else TASK_MISSING
            self._sync()
        return TaskDeletionResult(
            deleted=[job_id for job_id in deletion.job_ids if job_id in hidden],
            skipped=[
                SkippedTask(job_id=job_id, reason=reasons[job_id])
                for job_id in deletion.job_ids
                if job_id in reasons
            ],
        )

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
            self._sync()
            # 지운 작업의 ID 도 다시 쓰지 않는다. 남은 단계, 명령, 매핑 기록이 새 작업에 섞이지 않게.
            used = {task.job_id for task in self._config.migrations} | self._repository.used_ids()
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
            self._registered.add(job_id)
        return MigrationCreated(job_id=job_id, label=route_label(source, target))
