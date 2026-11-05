from datetime import datetime
from typing import Protocol

from controlplane.domain.fleet import EnvironmentSnapshot, MigrationState, WorkerSample
from controlplane.schemas.migrations import DataSource


class FleetSource(Protocol):
    mode: DataSource

    def list_environments(self) -> list[EnvironmentSnapshot]: ...

    def list_migrations(self) -> list[MigrationState]: ...

    def list_samples(self, job_id: str, since: datetime, until: datetime) -> list[WorkerSample]: ...
