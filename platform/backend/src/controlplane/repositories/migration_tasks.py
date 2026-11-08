import threading
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from psycopg.types.json import Jsonb

from controlplane.domain.fleet import MigrationConfig
from controlplane.repositories.releases import ReleaseStore
from controlplane.schemas.migrations import ACTIVE_COMMAND_STATUSES

VISIBLE = "hidden_at IS NULL"


class TaskRepository(Protocol):
    def list(self) -> list[MigrationConfig]: ...

    def add(self, task: MigrationConfig, created_by: str | None) -> None: ...

    def used_ids(self) -> set[str]: ...

    def hide(
        self, job_ids: Sequence[str], hidden_by: str, hidden_at: datetime
    ) -> tuple[set[str], set[str]]: ...


class MigrationTaskJournal:
    def __init__(self, releases: ReleaseStore) -> None:
        self._releases = releases

    def list(self) -> list[MigrationConfig]:
        with self._releases.connect() as conn:
            rows = conn.execute(
                f"SELECT config FROM fleet_migration_tasks WHERE {VISIBLE} "
                "ORDER BY created_at, job_id"
            ).fetchall()
        return [MigrationConfig.model_validate(row["config"]) for row in rows]

    def add(self, task: MigrationConfig, created_by: str | None) -> None:
        with self._releases.connect() as conn:
            conn.execute(
                "INSERT INTO fleet_migration_tasks(job_id, config, created_by) VALUES(%s,%s,%s)",
                (task.job_id, Jsonb(task.model_dump(mode="json")), created_by),
            )

    def used_ids(self) -> set[str]:
        """숨긴 작업까지 포함한 ID. 새 작업이 지운 작업의 ID 와 남은 기록을 이어받지 않게 한다."""
        with self._releases.connect() as conn:
            rows = conn.execute("SELECT job_id FROM fleet_migration_tasks").fetchall()
        return {row["job_id"] for row in rows}

    def hide(
        self, job_ids: Sequence[str], hidden_by: str, hidden_at: datetime
    ) -> tuple[set[str], set[str]]:
        """보이는 작업 중 대기, 실행 중 명령이 없는 것만 숨기고 (숨긴 것, 남긴 것)을 돌려준다.

        행을 지우지 않는다. 작업 ID 를 다시 쓰지 않고, 남은 단계, 명령, 매핑 기록이 새 작업에
        섞이지 않으며, 누가 언제 지웠는지 남는다.
        """
        active = list(ACTIVE_COMMAND_STATUSES)
        with self._releases.connect() as conn:
            hidden = conn.execute(
                "UPDATE fleet_migration_tasks t SET hidden_at=%s, hidden_by=%s "
                f"WHERE t.job_id = ANY(%s) AND t.{VISIBLE} AND NOT EXISTS ("
                "SELECT 1 FROM fleet_cutover_commands c "
                "WHERE c.job_id = t.job_id AND c.status = ANY(%s)) "
                "RETURNING t.job_id",
                (hidden_at, hidden_by, list(job_ids), active),
            ).fetchall()
            kept = conn.execute(
                f"SELECT job_id FROM fleet_migration_tasks WHERE job_id = ANY(%s) AND {VISIBLE}",
                (list(job_ids),),
            ).fetchall()
        return {row["job_id"] for row in hidden}, {row["job_id"] for row in kept}


class MemoryMigrationTasks:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: list[MigrationConfig] = []
        self._hidden: dict[str, tuple[str, datetime]] = {}

    def list(self) -> list[MigrationConfig]:
        with self._lock:
            return [task for task in self._rows if task.job_id not in self._hidden]

    def add(self, task: MigrationConfig, created_by: str | None) -> None:
        with self._lock:
            self._rows.append(task)

    def used_ids(self) -> set[str]:
        with self._lock:
            return {task.job_id for task in self._rows}

    def hidden_by(self, job_id: str) -> str | None:
        with self._lock:
            hidden = self._hidden.get(job_id)
            return hidden[0] if hidden else None

    def hide(
        self, job_ids: Sequence[str], hidden_by: str, hidden_at: datetime
    ) -> tuple[set[str], set[str]]:
        with self._lock:
            visible = {task.job_id for task in self._rows if task.job_id not in self._hidden}
            hidden = visible & set(job_ids)
            for job_id in hidden:
                self._hidden[job_id] = (hidden_by, hidden_at)
            return hidden, set()
