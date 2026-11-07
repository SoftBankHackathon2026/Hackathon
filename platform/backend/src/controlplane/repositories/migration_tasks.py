import threading
from typing import Protocol

from psycopg.types.json import Jsonb

from controlplane.domain.fleet import MigrationConfig
from controlplane.repositories.releases import ReleaseStore


class TaskRepository(Protocol):
    def list(self) -> list[MigrationConfig]: ...

    def add(self, task: MigrationConfig, created_by: str | None) -> None: ...

    def remove(self, job_id: str) -> bool: ...


class MigrationTaskJournal:
    def __init__(self, releases: ReleaseStore) -> None:
        self._releases = releases

    def list(self) -> list[MigrationConfig]:
        with self._releases.connect() as conn:
            rows = conn.execute(
                "SELECT config FROM fleet_migration_tasks ORDER BY created_at, job_id"
            ).fetchall()
        return [MigrationConfig.model_validate(row["config"]) for row in rows]

    def add(self, task: MigrationConfig, created_by: str | None) -> None:
        with self._releases.connect() as conn:
            conn.execute(
                "INSERT INTO fleet_migration_tasks(job_id, config, created_by) VALUES(%s,%s,%s)",
                (task.job_id, Jsonb(task.model_dump(mode="json")), created_by),
            )

    def remove(self, job_id: str) -> bool:
        with self._releases.connect() as conn:
            conn.execute("DELETE FROM fleet_cutover_commands WHERE job_id=%s", (job_id,))
            conn.execute("DELETE FROM fleet_data_mappings WHERE job_id=%s", (job_id,))
            row = conn.execute(
                "DELETE FROM fleet_migration_tasks WHERE job_id=%s RETURNING job_id", (job_id,)
            ).fetchone()
        return row is not None


class MemoryMigrationTasks:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: list[MigrationConfig] = []

    def list(self) -> list[MigrationConfig]:
        with self._lock:
            return list(self._rows)

    def add(self, task: MigrationConfig, created_by: str | None) -> None:
        with self._lock:
            self._rows.append(task)

    def remove(self, job_id: str) -> bool:
        with self._lock:
            before = len(self._rows)
            self._rows = [task for task in self._rows if task.job_id != job_id]
            return len(self._rows) != before
