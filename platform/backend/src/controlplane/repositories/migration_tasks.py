import threading
from typing import Protocol

from psycopg.types.json import Jsonb

from controlplane.domain.fleet import MigrationConfig
from controlplane.repositories.releases import ReleaseStore


class TaskRepository(Protocol):
    def list(self) -> list[MigrationConfig]: ...

    def add(self, task: MigrationConfig, created_by: str | None) -> None: ...


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
