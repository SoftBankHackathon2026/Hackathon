"""Durable app deletion journal. The API records the request; the environment's own
provider worker removes cloud resources one bounded step per tick.

requested -> <provider steps> -> deleted          (failed: stopped, safe to request again)

Each step re-reads the cloud before writing, so a lost response or a retried request
continues instead of failing on what is already gone. Deletion and releases share the
release intake lock: an environment never deletes and deploys at the same time.
"""

import logging
import re
import secrets

from psycopg.types.json import Jsonb

from controlplane.core import db
from controlplane.core.errors import RequestConflict
from controlplane.core.locks import AdvisoryLock, xact_lock
from controlplane.domain.models import Runtime

LOG = logging.getLogger(__name__)
ACTIVE = "state NOT IN ('deleted','failed')"


class DeletionStore:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self):
        return db.connect(self.dsn)

    def request(
        self,
        *,
        application: str,
        environment_id: str,
        runtime: Runtime,
        delete_data: bool,
        key: str,
        team: str | None = None,
        created_by: str | None = None,
    ) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
            raise ValueError("Invalid idempotency key")
        with self.connect() as conn:
            # Same lock as release intake: no deploy can start while this is checked.
            xact_lock(conn, AdvisoryLock.RELEASE_INTAKE)
            old = conn.execute(
                "SELECT * FROM app_deletions WHERE idempotency_key=%s", (key,)
            ).fetchone()
            if old:
                if (old["environment_id"], old["delete_data"]) != (environment_id, delete_data):
                    raise RequestConflict("같은 요청 키에 다른 삭제 내용이 있습니다.")
                return old
            if conn.execute(
                "SELECT 1 FROM releases WHERE environment_id=%s AND state NOT IN ('succeeded','failed')",
                (environment_id,),
            ).fetchone():
                raise RequestConflict(
                    "진행 중이거나 결과 확인이 필요한 배포가 있습니다. 배포가 끝난 뒤 삭제하세요."
                )
            if conn.execute(
                f"SELECT 1 FROM app_deletions WHERE environment_id=%s AND {ACTIVE}",
                (environment_id,),
            ).fetchone():
                raise RequestConflict("이미 앱을 삭제하는 중입니다.")
            row = conn.execute(
                """INSERT INTO app_deletions(id,application,environment_id,runtime,idempotency_key,
                   delete_data,state,team,created_by) VALUES(%s,%s,%s,%s,%s,%s,'requested',%s,%s)
                   RETURNING *""",
                (
                    secrets.token_hex(12),
                    application,
                    environment_id,
                    Runtime(runtime),
                    key,
                    delete_data,
                    team,
                    created_by,
                ),
            ).fetchone()
            conn.execute(
                "INSERT INTO app_deletion_events(deletion_id,state,message) VALUES(%s,'requested',%s)",
                (row["id"], "앱 삭제 요청을 저장했습니다."),
            )
            return row

    def latest(self, environment_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM app_deletions WHERE environment_id=%s ORDER BY created_at DESC LIMIT 1",
                (environment_id,),
            ).fetchone()
            if row:
                row["events"] = conn.execute(
                    "SELECT state,message,created_at FROM app_deletion_events WHERE deletion_id=%s ORDER BY id",
                    (row["id"],),
                ).fetchall()
            return row

    def latest_all(self) -> dict[str, dict]:
        """Latest deletion per environment, for decorating the environment list."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT ON (environment_id) * FROM app_deletions
                   ORDER BY environment_id, created_at DESC"""
            ).fetchall()
            events = conn.execute(
                """SELECT deletion_id,state,message,created_at FROM app_deletion_events
                   WHERE deletion_id = ANY(%s) ORDER BY id""",
                ([row["id"] for row in rows],),
            ).fetchall()
        for row in rows:
            row["events"] = [e for e in events if e["deletion_id"] == row["id"]]
        return {row["environment_id"]: row for row in rows}

    @staticmethod
    def has_active(conn, environment_id: str) -> bool:
        row = conn.execute(
            f"SELECT 1 FROM app_deletions WHERE environment_id=%s AND {ACTIVE}",
            (environment_id,),
        ).fetchone()
        return row is not None

    @staticmethod
    def add_event(conn, row, message: str) -> None:
        conn.execute(
            "INSERT INTO app_deletion_events(deletion_id,state,message) VALUES(%s,%s,%s)",
            (row["id"], row["state"], message),
        )

    @staticmethod
    def next(conn, environment_id: str):
        row = conn.execute(
            f"""SELECT * FROM app_deletions WHERE environment_id=%s AND {ACTIVE}
                AND next_attempt_at <= now() LIMIT 1""",
            (environment_id,),
        ).fetchone()
        conn.commit()
        return row

    @staticmethod
    def transition(conn, row, state, message, *, checkpoint=None, error_code=None):
        cp = checkpoint if checkpoint is not None else row["checkpoint"]
        result = conn.execute(
            """UPDATE app_deletions SET state=%s,checkpoint=%s,error_code=%s,updated_at=now(),
               next_attempt_at=now() WHERE id=%s AND state=%s RETURNING id""",
            (state, Jsonb(cp), error_code, row["id"], row["state"]),
        ).fetchone()
        if not result:
            conn.rollback()
            raise RequestConflict("삭제 상태가 변경됐습니다.")
        conn.execute(
            "INSERT INTO app_deletion_events(deletion_id,state,message) VALUES(%s,%s,%s)",
            (row["id"], state, message),
        )
        if state == "deleted":
            # History stays in the journal for audit; it no longer describes a live app.
            conn.execute(
                "UPDATE releases SET archived=%s WHERE environment_id=%s AND archived IS NULL",
                ("all" if row["delete_data"] else "app", row["environment_id"]),
            )
        conn.commit()
        row.update(state=state, checkpoint=cp)

    @staticmethod
    def defer(conn, row, *, seconds: float, checkpoint=None):
        cp = checkpoint if checkpoint is not None else row["checkpoint"]
        conn.execute(
            """UPDATE app_deletions SET next_attempt_at=now() + %s * interval '1 second',
               checkpoint=%s, updated_at=now() WHERE id=%s AND state=%s""",
            (seconds, Jsonb(cp), row["id"], row["state"]),
        )
        conn.commit()
        row["checkpoint"] = cp
