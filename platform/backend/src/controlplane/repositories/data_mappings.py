import threading
from typing import Protocol

from psycopg.types.json import Jsonb

from controlplane.repositories.releases import ReleaseStore
from controlplane.schemas.data_mapping import SavedMapping


class MappingRepository(Protocol):
    def get(self, job_id: str) -> SavedMapping | None: ...

    def put(self, mapping: SavedMapping) -> None: ...


class DataMappingJournal:
    def __init__(self, releases: ReleaseStore) -> None:
        self._releases = releases

    def get(self, job_id: str) -> SavedMapping | None:
        with self._releases.connect() as conn:
            row = conn.execute(
                "SELECT job_id, inventory_hash, bindings, confirmed_by, confirmed_at "
                "FROM fleet_data_mappings WHERE job_id=%s",
                (job_id,),
            ).fetchone()
        return SavedMapping.model_validate(dict(row)) if row else None

    def put(self, mapping: SavedMapping) -> None:
        with self._releases.connect() as conn:
            conn.execute(
                "INSERT INTO fleet_data_mappings"
                "(job_id, inventory_hash, bindings, confirmed_by, confirmed_at) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(job_id) DO UPDATE SET "
                "inventory_hash=excluded.inventory_hash, bindings=excluded.bindings, "
                "confirmed_by=excluded.confirmed_by, confirmed_at=excluded.confirmed_at",
                (
                    mapping.job_id,
                    mapping.inventory_hash,
                    Jsonb([b.model_dump(mode="json") for b in mapping.bindings]),
                    mapping.confirmed_by,
                    mapping.confirmed_at,
                ),
            )


class MemoryDataMappings:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, SavedMapping] = {}

    def get(self, job_id: str) -> SavedMapping | None:
        with self._lock:
            return self._rows.get(job_id)

    def put(self, mapping: SavedMapping) -> None:
        with self._lock:
            self._rows[mapping.job_id] = mapping
