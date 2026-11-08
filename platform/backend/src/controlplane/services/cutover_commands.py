"""전환 제어 명령(시작·승인·중단·재시도).

플랫폼은 클러스터·DB 에 쓰지 않는다. 화면이 만든 명령을 저장하고, 온프렘 러너 Pod 의 보고기가
`claim` 으로 가져가 `python -m migration.cutover ...` 를 실행한 뒤 `finish` 로 결과를 올린다.
"""

import secrets
from collections.abc import Callable
from datetime import UTC, datetime

from controlplane.core.errors import InvalidRequest, OperationNotFound, RequestConflict
from controlplane.domain.fleet import FleetConfig
from controlplane.repositories.cutover_commands import COMMAND_IN_PROGRESS, CommandRepository
from controlplane.schemas.accounts import Identity
from controlplane.schemas.migrations import (
    ClaimedCommand,
    CommandCreate,
    CommandResult,
    CommandStatus,
    CutoverCommand,
    MigrationEvent,
)

MIGRATION_NOT_FOUND = "등록된 마이그레이션 작업이 없습니다."
COMMAND_NOT_FOUND = "해당 명령이 없습니다."
COMMAND_NOT_RUNNING = "실행 중인 명령이 아니어서 결과를 기록할 수 없습니다."
APPROVAL_ID_REQUIRED = "승인에는 approval_id 가 필요합니다."
EXIT_OK = 0
EXIT_WAITING_APPROVAL = 3
ACTION_TITLES = {
    "run": "시작",
    "approve": "승인",
    "abort": "중단·원복",
    "retry": "재시도",
    "force_retry": "강제 재개",
}
RESULT_TITLES: dict[CommandStatus, str] = {
    "succeeded": "성공",
    "waiting_approval": "승인 대기",
    "failed": "실패",
}


def status_for_exit(exit_code: int) -> CommandStatus:
    if exit_code == EXIT_OK:
        return "succeeded"
    if exit_code == EXIT_WAITING_APPROVAL:
        return "waiting_approval"
    return "failed"


def command_events(commands: list[CutoverCommand]) -> list[MigrationEvent]:
    events = []
    for command in commands:
        title = ACTION_TITLES.get(command.action, command.action)
        who = command.requested_by or "운영 화면"
        events.append(
            MigrationEvent(
                at=command.requested_at,
                kind="command",
                title=f"명령 요청: {title} (by {who})",
                detail=command.note or command.action,
            )
        )
        if command.finished_at is not None and command.status in RESULT_TITLES:
            events.append(
                MigrationEvent(
                    at=command.finished_at,
                    kind="warning" if command.status == "failed" else "command",
                    title=f"명령 결과: {title} {RESULT_TITLES[command.status]}",
                    detail=f"exit {command.exit_code}, stage {command.stage or '-'}",
                )
            )
    return events


class CutoverCommandService:
    def __init__(
        self,
        config: FleetConfig,
        store: CommandRepository,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        blocker: Callable[[str], str | None] = lambda job_id: None,
    ) -> None:
        self._config = config
        self._store = store
        self._now = now
        self._blocker = blocker

    def _check_job(self, job_id: str) -> None:
        if self._config.migration(job_id) is None:
            raise OperationNotFound(MIGRATION_NOT_FOUND)

    def request(
        self, job_id: str, body: CommandCreate, identity: Identity | None
    ) -> CutoverCommand:
        self._check_job(job_id)
        approval_id = (body.approval_id or "").strip() or None
        if body.action == "approve" and approval_id is None:
            raise InvalidRequest(APPROVAL_ID_REQUIRED)
        if self._store.active(job_id) is not None:
            raise RequestConflict(COMMAND_IN_PROGRESS)
        if body.action == "run":
            blocked = self._blocker(job_id)
            if blocked:
                raise RequestConflict(blocked)
        command = CutoverCommand(
            id=f"cmd_{secrets.token_hex(8)}",
            job_id=job_id,
            action=body.action,
            approval_id=approval_id if body.action == "approve" else None,
            note=(body.note or "").strip() or None,
            requested_by=identity.email if identity else None,
            requested_at=self._now(),
            status="pending",
        )
        return self._store.create(command)

    def list_commands(self, job_id: str) -> list[CutoverCommand]:
        self._check_job(job_id)
        return self._store.list(job_id)

    def events(self, job_id: str) -> list[MigrationEvent]:
        return command_events(self._store.list(job_id))

    def has_active(self, job_id: str) -> bool:
        return self._store.active(job_id) is not None

    def claim(self, job_id: str) -> CutoverCommand | None:
        self._check_job(job_id)
        return self._store.claim(job_id, self._now())

    def claim_any(self) -> ClaimedCommand | None:
        jobs = [task.job_id for task in self._config.migrations]
        command = self._store.claim_any(jobs, self._now())
        if command is None:
            return None
        task = self._config.migration(command.job_id)
        return ClaimedCommand(
            command=command,
            job_id=task.job_id,
            source_env=task.source_env,
            target_env=task.target_env,
            data_source=task.data_source,
            data_target=task.data_target,
            source_provider=self._provider(task.source_env, task.source_provider),
            target_provider=self._provider(task.target_env, task.target_provider),
            apps=list(task.apps),
        )

    def _provider(self, env_id: str, given: str | None) -> str | None:
        try:
            return self._config.environment(env_id).provider
        except KeyError:
            return given

    def finish(self, job_id: str, command_id: str, result: CommandResult) -> CutoverCommand:
        self._check_job(job_id)
        updated = self._store.finish(
            job_id,
            command_id,
            status=status_for_exit(result.exit_code),
            exit_code=result.exit_code,
            stage=result.stage,
            log_tail=result.log_tail,
            finished_at=self._now(),
        )
        if updated is None:
            if self._store.get(job_id, command_id) is None:
                raise OperationNotFound(COMMAND_NOT_FOUND)
            raise RequestConflict(COMMAND_NOT_RUNNING)
        return updated
