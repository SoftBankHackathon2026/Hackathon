from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from controlplane.repositories.releases import ReleaseStore

Row = dict[str, Any]
VIEW_COLUMNS = "id, kind, status, name, inventory, created_at, expires_at, registered_at"


class EnrollmentStore:
    """환경 등록 기록. token_hash·access 는 이 저장소 밖으로 꺼내지 않는다(access 는 수집기 전용)."""

    def __init__(self, releases: ReleaseStore) -> None:
        self._releases = releases

    def connect(self):
        return self._releases.connect()

    def create(
        self,
        enrollment_id: str,
        kind: str,
        token_hash: str,
        expires_at: datetime,
        *,
        team: str | None,
        external_id: str | None,
    ) -> Row:
        with self.connect() as conn:
            return conn.execute(
                "INSERT INTO environment_enrollments(id, kind, status, token_hash, external_id, team, expires_at) "
                f"VALUES(%s,%s,'pending',%s,%s,%s,%s) RETURNING {VIEW_COLUMNS}",
                (enrollment_id, kind, token_hash, external_id, team, expires_at),
            ).fetchone()

    def get(self, enrollment_id: str) -> Row | None:
        with self.connect() as conn:
            return conn.execute(
                f"SELECT {VIEW_COLUMNS}, team FROM environment_enrollments WHERE id=%s",
                (enrollment_id,),
            ).fetchone()

    def secret_fields(self, enrollment_id: str) -> Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT kind, status, token_hash, external_id, expires_at "
                "FROM environment_enrollments WHERE id=%s",
                (enrollment_id,),
            ).fetchone()

    def list(self, team: str | None, limit: int = 50) -> list[Row]:
        with self.connect() as conn:
            return conn.execute(
                f"SELECT {VIEW_COLUMNS} FROM environment_enrollments "
                "WHERE team IS NOT DISTINCT FROM %s ORDER BY created_at DESC LIMIT %s",
                (team, limit),
            ).fetchall()

    def complete(self, enrollment_id: str, name: str, inventory: Row, access: Row) -> Row | None:
        """대기 중이고 만료 전인 등록만 한 번 완료한다. 이미 쓰인 토큰으로는 바꿀 수 없다."""
        with self.connect() as conn:
            return conn.execute(
                "UPDATE environment_enrollments SET status='registered', name=%s, inventory=%s, "
                "access=%s, registered_at=now() "
                "WHERE id=%s AND status='pending' AND expires_at > now() "
                f"RETURNING {VIEW_COLUMNS}",
                (name, Jsonb(inventory), Jsonb(access), enrollment_id),
            ).fetchone()
