"""전환 명령 저장소. 플랫폼 DB(fleet_cutover_commands)와 시뮬레이션용 메모리 구현.

플랫폼은 명령을 저장만 한다. 러너 Pod 의 보고기가 claim 으로 가져가 실행하고 result 로 끝낸다.
"""

import threading
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from psycopg.errors import UniqueViolation

from controlplane.core.errors import RequestConflict
from controlplane.repositories.releases import ReleaseStore
from controlplane.schemas.migrations import ACTIVE_COMMAND_STATUSES, CommandStatus, CutoverCommand

COMMAND_IN_PROGRESS = (
    "이 작업에 아직 끝나지 않은 명령이 있습니다. 결과를 기다린 뒤 다시 요청하세요."
)
# 지운(숨긴) 작업의 명령은 다른 프로세스가 아직 작업을 들고 있어도 러너에게 넘기지 않는다.
NOT_HIDDEN = "job_id NOT IN (SELECT job_id FROM fleet_migration_tasks WHERE hidden_at IS NOT NULL)"
COLUMNS = (
    "id, job_id, action, approval_id, note, requested_by, requested_at, status, "
    "started_at, finished_at, exit_code, stage, log_tail"
)


class CommandRepository(Protocol):
    def create(self, command: CutoverCommand) -> CutoverCommand: ...

    def list(self, job_id: str) -> list[CutoverCommand]: ...

    def active(self, job_id: str) -> CutoverCommand | None: ...

    def get(self, job_id: str, command_id: str) -> CutoverCommand | None: ...

    def claim(self, job_id: str, started_at: datetime) -> CutoverCommand | None: ...

    def claim_any(self, job_ids: Sequence[str], started_at: datetime) -> CutoverCommand | None: ...

    def finish(
        self,
        job_id: str,
        command_id: str,
        *,
        status: CommandStatus,
        exit_code: int,
        stage: str | None,
        log_tail: str,
        finished_at: datetime,
    ) -> CutoverCommand | None: ...


class CutoverCommandJournal:
    def __init__(self, releases: ReleaseStore) -> None:
        self._releases = releases

    def connect(self):
        return self._releases.connect()

    def create(self, command: CutoverCommand) -> CutoverCommand:
        try:
            with self.connect() as conn:
                conn.execute(
                    f"INSERT INTO fleet_cutover_commands({COLUMNS}) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        command.id,
                        command.job_id,
                        command.action,
                        command.approval_id,
                        command.note,
                        command.requested_by,
                        command.requested_at,
                        command.status,
                        command.started_at,
                        command.finished_at,
                        command.exit_code,
                        command.stage,
                        command.log_tail,
                    ),
                )
        except UniqueViolation:
            raise RequestConflict(COMMAND_IN_PROGRESS) from None
        return command

    def list(self, job_id: str) -> list[CutoverCommand]:
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT {COLUMNS} FROM fleet_cutover_commands WHERE job_id=%s "
                "ORDER BY requested_at DESC, id DESC",
                (job_id,),
            ).fetchall()
        return [CutoverCommand.model_validate(row) for row in rows]

    def active(self, job_id: str) -> CutoverCommand | None:
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT {COLUMNS} FROM fleet_cutover_commands "
                "WHERE job_id=%s AND status = ANY(%s) ORDER BY requested_at LIMIT 1",
                (job_id, list(ACTIVE_COMMAND_STATUSES)),
            ).fetchone()
        return CutoverCommand.model_validate(row) if row else None

    def get(self, job_id: str, command_id: str) -> CutoverCommand | None:
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT {COLUMNS} FROM fleet_cutover_commands WHERE job_id=%s AND id=%s",
                (job_id, command_id),
            ).fetchone()
        return CutoverCommand.model_validate(row) if row else None

    def claim_any(self, job_ids: Sequence[str], started_at: datetime) -> CutoverCommand | None:
        with self.connect() as conn:
            row = conn.execute(
                "UPDATE fleet_cutover_commands SET status='running', started_at=%s "
                "WHERE id = (SELECT id FROM fleet_cutover_commands "
                f"WHERE job_id = ANY(%s) AND status='pending' AND {NOT_HIDDEN} "
                "ORDER BY requested_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
                f"RETURNING {COLUMNS}",
                (started_at, list(job_ids)),
            ).fetchone()
        return CutoverCommand.model_validate(row) if row else None

    def claim(self, job_id: str, started_at: datetime) -> CutoverCommand | None:
        with self.connect() as conn:
            row = conn.execute(
                "UPDATE fleet_cutover_commands SET status='running', started_at=%s "
                "WHERE id = (SELECT id FROM fleet_cutover_commands "
                f"WHERE job_id=%s AND status='pending' AND {NOT_HIDDEN} "
                "ORDER BY requested_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
                f"RETURNING {COLUMNS}",
                (started_at, job_id),
            ).fetchone()
        return CutoverCommand.model_validate(row) if row else None

    def finish(
        self,
        job_id: str,
        command_id: str,
        *,
        status: CommandStatus,
        exit_code: int,
        stage: str | None,
        log_tail: str,
        finished_at: datetime,
    ) -> CutoverCommand | None:
        with self.connect() as conn:
            row = conn.execute(
                "UPDATE fleet_cutover_commands SET status=%s, exit_code=%s, stage=%s, log_tail=%s, "
                "finished_at=%s WHERE job_id=%s AND id=%s AND status='running' "
                f"RETURNING {COLUMNS}",
                (status, exit_code, stage, log_tail, finished_at, job_id, command_id),
            ).fetchone()
        return CutoverCommand.model_validate(row) if row else None


class MemoryCutoverCommands:
    """DB 가 없는 시뮬레이션 모드용. 프로세스가 끝나면 사라진다."""

    def __init__(self) -> None:
        self._rows: dict[str, list[CutoverCommand]] = {}
        self._lock = threading.Lock()

    def create(self, command: CutoverCommand) -> CutoverCommand:
        with self._lock:
            if any(c.status in ACTIVE_COMMAND_STATUSES for c in self._rows.get(command.job_id, [])):
                raise RequestConflict(COMMAND_IN_PROGRESS)
            self._rows.setdefault(command.job_id, []).append(command)
        return command

    def list(self, job_id: str) -> list[CutoverCommand]:
        with self._lock:
            return sorted(self._rows.get(job_id, []), key=lambda c: c.requested_at, reverse=True)

    def active(self, job_id: str) -> CutoverCommand | None:
        with self._lock:
            return next(
                (c for c in self._rows.get(job_id, []) if c.status in ACTIVE_COMMAND_STATUSES),
                None,
            )

    def get(self, job_id: str, command_id: str) -> CutoverCommand | None:
        with self._lock:
            return next((c for c in self._rows.get(job_id, []) if c.id == command_id), None)

    def _replace(self, job_id: str, updated: CutoverCommand) -> CutoverCommand:
        rows = self._rows[job_id]
        rows[[c.id for c in rows].index(updated.id)] = updated
        return updated

    def claim_any(self, job_ids: Sequence[str], started_at: datetime) -> CutoverCommand | None:
        with self._lock:
            pending = sorted(
                (
                    c
                    for job_id in job_ids
                    for c in self._rows.get(job_id, [])
                    if c.status == "pending"
                ),
                key=lambda c: c.requested_at,
            )
            if not pending:
                return None
            return self._replace(
                pending[0].job_id,
                pending[0].model_copy(update={"status": "running", "started_at": started_at}),
            )

    def claim(self, job_id: str, started_at: datetime) -> CutoverCommand | None:
        with self._lock:
            pending = sorted(
                (c for c in self._rows.get(job_id, []) if c.status == "pending"),
                key=lambda c: c.requested_at,
            )
            if not pending:
                return None
            return self._replace(
                job_id,
                pending[0].model_copy(update={"status": "running", "started_at": started_at}),
            )

    def finish(
        self,
        job_id: str,
        command_id: str,
        *,
        status: CommandStatus,
        exit_code: int,
        stage: str | None,
        log_tail: str,
        finished_at: datetime,
    ) -> CutoverCommand | None:
        with self._lock:
            current = next((c for c in self._rows.get(job_id, []) if c.id == command_id), None)
            if current is None or current.status != "running":
                return None
            return self._replace(
                job_id,
                current.model_copy(
                    update={
                        "status": status,
                        "exit_code": exit_code,
                        "stage": stage,
                        "log_tail": log_tail,
                        "finished_at": finished_at,
                    }
                ),
            )
