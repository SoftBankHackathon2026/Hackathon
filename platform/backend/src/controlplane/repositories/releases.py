"""PostgreSQL journal. Writes are committed before provider side effects."""

import hashlib
import json
import secrets
from contextlib import contextmanager
from importlib.resources import files

from psycopg.types.json import Jsonb

from controlplane.core import db
from controlplane.core.errors import RequestConflict
from controlplane.core.locks import (
    AdvisoryLock,
    session_lock,
    session_unlock,
    try_session_lock,
    try_shared_xact_lock,
    xact_lock,
)
from controlplane.domain.models import DeploymentRequest, Runtime

MIGRATIONS_PACKAGE = "migrations"


class ReleaseStore:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self):
        return db.connect(self.dsn)

    def migrate(self):
        scripts = sorted(
            (
                path
                for path in files("controlplane").joinpath(MIGRATIONS_PACKAGE).iterdir()
                if path.name.endswith(".sql")
            ),
            key=lambda path: path.name,
        )
        with self.connect() as conn:
            xact_lock(conn, AdvisoryLock.MIGRATION)
            for script in scripts:
                conn.execute(script.read_text())

    def enqueue(
        self,
        request: DeploymentRequest,
        key: str,
        binding_fingerprint: str,
        *,
        deadline=None,
        timeout_seconds=720,
        runtime: Runtime,
        team: str | None = None,
        created_by: str | None = None,
    ):
        if not 60 <= timeout_seconds <= 86400:
            raise ValueError("Release timeout must be bounded")
        runtime = Runtime(runtime)
        payload = request.model_dump(mode="json")
        fingerprint = hashlib.sha256(
            json.dumps(
                {"request": payload, "binding": binding_fingerprint}, sort_keys=True
            ).encode()
        ).hexdigest()
        with self.connect() as conn:
            # Fail immediately during a configuration switch: waiting could let
            # a handler resume later with the old provider's fingerprint.
            if not try_shared_xact_lock(conn, AdvisoryLock.CONFIGURATION):
                raise RequestConflict("플랫폼 설정을 갱신 중입니다. 잠시 후 다시 배포하세요.")
            # This DB lock serializes
            # duplicate keys; a unique active-release index is the second defense.
            xact_lock(conn, AdvisoryLock.RELEASE_INTAKE)
            existing = conn.execute(
                "SELECT * FROM releases WHERE idempotency_key=%s", (key,)
            ).fetchone()
            if existing:
                if (
                    existing["fingerprint"] != fingerprint
                    or existing["runtime"] != runtime
                    or existing["team"] != team
                ):
                    raise RequestConflict("동일 요청 키에 다른 배포 내용이 있습니다.")
                return existing
            if conn.execute(
                "SELECT 1 FROM releases WHERE application=%s AND environment_id=%s AND state NOT IN ('succeeded','failed')",
                (request.application, request.environment_id),
            ).fetchone():
                raise RequestConflict("진행 중이거나 결과 확인이 필요한 배포가 있습니다.")
            if conn.execute(
                "SELECT 1 FROM app_deletions WHERE environment_id=%s AND state NOT IN ('deleted','failed')",
                (request.environment_id,),
            ).fetchone():
                raise RequestConflict("앱을 삭제하는 중입니다. 삭제가 끝난 뒤 다시 배포하세요.")
            row = conn.execute(
                """INSERT INTO releases(id,application,environment_id,idempotency_key,fingerprint,request,binding_fingerprint,state,deadline,runtime,team,created_by)
                VALUES(%s,%s,%s,%s,%s,%s,%s,'queued',LEAST(now()+%s*interval '1 second', %s),%s,%s,%s) RETURNING *""",
                (
                    secrets.token_hex(12),
                    request.application,
                    request.environment_id,
                    key,
                    fingerprint,
                    Jsonb(payload),
                    binding_fingerprint,
                    timeout_seconds,
                    deadline,
                    runtime,
                    team,
                    created_by,
                ),
            ).fetchone()
            conn.execute(
                "INSERT INTO release_events(release_id,state,message) VALUES(%s,'queued','배포 요청을 저장했습니다.')",
                (row["id"],),
            )
            return row

    def request_action(
        self, release_id, action, *, application, environment_id, binding_fingerprint
    ):
        from controlplane.execution.gcp.worker import available_actions

        # Serialize with worker ticks so an action cannot overwrite a newer checkpoint.
        with self.worker_lock() as conn:
            if conn is None:
                raise RequestConflict("실행기가 처리 중입니다. 잠시 후 다시 시도하세요.")
            row = conn.execute(
                "SELECT *, now() > deadline AS expired FROM releases WHERE id=%s", (release_id,)
            ).fetchone()
            if (
                not row
                or row["application"] != application
                or row["environment_id"] != environment_id
                or row["binding_fingerprint"] != binding_fingerprint
            ):
                raise RequestConflict("현재 등록 환경과 일치하는 배포가 아닙니다.")
            if row["expired"] or action not in available_actions(row):
                raise RequestConflict("현재 단계에서는 이 작업을 실행할 수 없습니다.")
            cp = dict(row["checkpoint"])
            if cp.get("action") and cp["action"] != action:
                raise RequestConflict("이미 다른 작업이 접수됐습니다.")
            cp["action"] = action
            self.transition(
                conn,
                row,
                row["state"],
                "수동 전환을 요청했습니다." if action == "promote" else "수동 복귀를 요청했습니다.",
                checkpoint=cp,
            )
            return row

    def list(self, *, runtime: Runtime | None = None, team: str | None = None):
        """`team` limits the history to one team's releases (login mode)."""
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM releases WHERE (%s::text IS NULL OR runtime=%s)
                AND (%s::text IS NULL OR team=%s) AND archived IS NULL
                ORDER BY created_at DESC LIMIT 50""",
                (runtime, runtime, team, team),
            ).fetchall()

    def list_with_events(
        self,
        *,
        team: str | None = None,
        environment_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        """History with each release's events in two queries (not one per release)."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM releases WHERE (%s::text IS NULL OR team=%s)
                AND (%s::text IS NULL OR environment_id=%s)
                AND archived IS NULL ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s""",
                (team, team, environment_id, environment_id, limit, offset),
            ).fetchall()
            events: dict[str, list] = {row["id"]: [] for row in rows}
            if rows:
                for event in conn.execute(
                    """SELECT release_id,state,message,created_at FROM release_events
                    WHERE release_id = ANY(%s) ORDER BY release_id, id""",
                    (list(events),),
                ).fetchall():
                    events[event.pop("release_id")].append(event)
            for row in rows:
                row["events"] = events[row["id"]]
            return rows

    def get(self, release_id: str):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM releases WHERE id=%s", (release_id,)).fetchone()
            if row:
                row["events"] = conn.execute(
                    "SELECT state,message,created_at FROM release_events WHERE release_id=%s ORDER BY id",
                    (release_id,),
                ).fetchall()
            return row

    @contextmanager
    def configuration_lock(self):
        """Hold provider configuration stable across intake, ticks and restarts."""
        with self.connect() as conn:
            # Wait for an existing tick/intake, but do not hang activation forever.
            conn.execute("SET LOCAL lock_timeout = '30s'")
            session_lock(conn, AdvisoryLock.CONFIGURATION)
            # Reuse the worker lock so older worker processes also finish their
            # current tick and stay paused throughout activation.
            session_lock(conn, AdvisoryLock.WORKER)
            conn.commit()
            try:
                yield
            finally:
                if not conn.closed:
                    session_unlock(conn, AdvisoryLock.WORKER)
                    session_unlock(conn, AdvisoryLock.CONFIGURATION)
                    conn.commit()

    @contextmanager
    def worker_lock(self):
        # Held for the whole tick, including external calls. Session crash releases
        # lock, but intent states remain and cannot blindly replay a mutation.
        with self.connect() as conn:
            acquired = try_session_lock(conn, AdvisoryLock.WORKER)
            conn.commit()
            try:
                yield conn if acquired else None
            finally:
                if acquired and not conn.closed:
                    session_unlock(conn, AdvisoryLock.WORKER)
                    conn.commit()

    @staticmethod
    def next(conn, *, runtime: Runtime, application=None, environment_id=None):
        row = conn.execute(
            """SELECT *, now() > deadline AS expired FROM releases
            WHERE runtime=%s AND state NOT IN ('succeeded','failed','uncertain')
              AND next_attempt_at <= now()
              AND (%s::text IS NULL OR application=%s)
              AND (%s::text IS NULL OR environment_id=%s)
            ORDER BY next_attempt_at, created_at LIMIT 1""",
            (runtime, application, application, environment_id, environment_id),
        ).fetchone()
        conn.commit()
        return row

    @staticmethod
    def defer(conn, row, *, seconds: float, checkpoint=None):
        """Schedule the next observation without flooding the event log or sleeping under lock."""
        if not 0 <= seconds <= 60:
            raise ValueError("Polling delay must be bounded")
        cp = checkpoint if checkpoint is not None else row["checkpoint"]
        result = conn.execute(
            """UPDATE releases SET next_attempt_at=now() + %s * interval '1 second',
               checkpoint=%s, updated_at=now() WHERE id=%s AND state=%s RETURNING id""",
            (seconds, Jsonb(cp), row["id"], row["state"]),
        ).fetchone()
        if not result:
            conn.rollback()
            raise RequestConflict("작업 상태가 변경됐습니다.")
        conn.commit()
        row["checkpoint"] = cp

    @staticmethod
    def transition(
        conn, row, state: str, message: str, *, checkpoint=None, error_code=None, url=None
    ):
        result = conn.execute(
            """UPDATE releases SET state=%s,checkpoint=%s,error_code=%s,service_url=COALESCE(%s,service_url),updated_at=now(),next_attempt_at=now()
            WHERE id=%s AND state=%s RETURNING id""",
            (
                state,
                Jsonb(checkpoint if checkpoint is not None else row["checkpoint"]),
                error_code,
                url,
                row["id"],
                row["state"],
            ),
        ).fetchone()
        if not result:
            conn.rollback()
            raise RequestConflict("작업 상태가 변경됐습니다.")
        conn.execute(
            "INSERT INTO release_events(release_id,state,message) VALUES(%s,%s,%s)",
            (row["id"], state, message),
        )
        conn.commit()
        row.update(
            state=state, checkpoint=checkpoint if checkpoint is not None else row["checkpoint"]
        )
