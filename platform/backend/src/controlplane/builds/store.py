import hashlib
import json
import secrets

from psycopg.types.json import Jsonb

from controlplane.builds.models import BuildState
from controlplane.core.errors import RequestConflict
from controlplane.core.locks import AdvisoryLock, xact_lock
from controlplane.repositories.releases import ReleaseStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_builds (
 id text PRIMARY KEY, idempotency_key text NOT NULL UNIQUE, fingerprint text NOT NULL,
 application text NOT NULL, environment_id text NOT NULL, request jsonb NOT NULL,
 state text NOT NULL DEFAULT 'queued', checkpoint jsonb NOT NULL DEFAULT '{}',
 image text, release_id text, error_code text, events jsonb NOT NULL DEFAULT '[]',
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
-- Set in login mode: the team that started the build and who requested it.
ALTER TABLE source_builds ADD COLUMN IF NOT EXISTS team text;
ALTER TABLE source_builds ADD COLUMN IF NOT EXISTS created_by text;
CREATE INDEX IF NOT EXISTS source_build_artifacts ON source_builds(application, environment_id, image)
 WHERE image IS NOT NULL;
"""


class BuildStore(ReleaseStore):
    def migrate(self):
        with self.connect() as conn:
            xact_lock(conn, AdvisoryLock.SOURCE_BUILDS)
            conn.execute(SCHEMA)

    def enqueue(self, request, max_pending=None, max_daily=None, *, team=None, created_by=None):
        payload = request.model_dump(mode="json")
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.connect() as conn:
            xact_lock(conn, AdvisoryLock.SOURCE_BUILDS)
            row = conn.execute(
                "SELECT * FROM source_builds WHERE idempotency_key=%s", (request.idempotency_key,)
            ).fetchone()
            if row:
                if row["fingerprint"] != fingerprint or row["team"] != team:
                    raise RequestConflict("같은 요청 키에 다른 빌드 내용이 있습니다.")
                return row
            self._check_quotas(conn, request, max_pending, max_daily)
            return self._insert(conn, request, payload, fingerprint, team, created_by)

    @staticmethod
    def _check_quotas(conn, request, max_pending, max_daily):
        if max_daily is not None:
            daily = conn.execute(
                "SELECT count(*) AS n FROM source_builds WHERE created_at > now() - interval '24 hours'"
            ).fetchone()["n"]
            if daily >= max_daily:
                raise RequestConflict("시연용 24시간 빌드 한도에 도달했습니다.")
        active = conn.execute(
            "SELECT 1 FROM source_builds WHERE application=%s AND environment_id=%s AND state NOT IN ('succeeded','failed')",
            (request.deployment.application, request.deployment.environment_id),
        ).fetchone()
        if active:
            raise RequestConflict("이 환경의 빌드가 진행 중이거나 결과 확인이 필요합니다.")
        if max_pending is not None:
            pending = conn.execute(
                "SELECT count(*) AS n FROM source_builds WHERE state NOT IN ('succeeded','failed','uncertain')"
            ).fetchone()["n"]
            if pending >= max_pending:
                raise RequestConflict(
                    "빌드 대기열이 가득 찼습니다. 진행 중인 빌드를 기다려 주세요."
                )

    @staticmethod
    def _insert(conn, request, payload, fingerprint, team, created_by):
        return conn.execute(
            """INSERT INTO source_builds(id,idempotency_key,fingerprint,application,environment_id,request,team,created_by)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                secrets.token_hex(12),
                request.idempotency_key,
                fingerprint,
                request.deployment.application,
                request.deployment.environment_id,
                Jsonb(payload),
                team,
                created_by,
            ),
        ).fetchone()

    def list(self, *, team=None, environment_id=None, limit=30, offset=0):
        with self.connect() as conn:
            return conn.execute(
                """SELECT * FROM source_builds WHERE (%s::text IS NULL OR team=%s)
                AND (%s::text IS NULL OR environment_id=%s)
                ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s""",
                (team, team, environment_id, environment_id, limit, offset),
            ).fetchall()

    def reconcile(self, build_id):
        """Resume observation of a known build; never repeat an ambiguous StartBuild."""
        with self.connect() as conn:
            xact_lock(conn, AdvisoryLock.SOURCE_BUILDS)
            row = conn.execute(
                "SELECT * FROM source_builds WHERE id=%s FOR UPDATE", (build_id,)
            ).fetchone()
            if (
                not row
                or row["state"] != BuildState.UNCERTAIN
                or row["error_code"] != "build_provider_unavailable"
                or not row["checkpoint"].get("build_id")
            ):
                raise RequestConflict("실행 ID가 확인된 조회 실패만 다시 확인할 수 있습니다.")
            checkpoint = dict(row["checkpoint"], retries=0)
            self.update(
                conn,
                row,
                BuildState.BUILDING,
                "기존 빌드의 상태 확인을 다시 시작합니다.",
                checkpoint=checkpoint,
            )
        return self.get(build_id)

    def get(self, build_id):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM source_builds WHERE id=%s", (build_id,)).fetchone()

    def update(
        self,
        conn,
        row,
        state,
        message,
        *,
        checkpoint=None,
        image=None,
        release_id=None,
        error_code=None,
    ):
        conn.execute(
            """UPDATE source_builds SET state=%s, checkpoint=%s, image=COALESCE(%s,image),
          release_id=COALESCE(%s,release_id),error_code=%s,updated_at=now(),
          events=events || jsonb_build_array(jsonb_build_object('state',%s::text,'message',%s::text,'at',now()))
          WHERE id=%s AND state=%s""",
            (
                str(state),
                Jsonb(checkpoint if checkpoint is not None else row["checkpoint"]),
                image,
                release_id,
                error_code,
                str(state),
                message,
                row["id"],
                row["state"],
            ),
        )
        conn.commit()


def approved_image(dsn, application, environment_id, image):
    """Only the build worker can publish an artifact; HTTP inputs cannot approve images."""
    store = BuildStore(dsn)
    with store.connect() as conn:
        if conn.execute("SELECT to_regclass('source_builds') AS name").fetchone()["name"] is None:
            return False
        return bool(
            conn.execute(
                "SELECT 1 FROM source_builds WHERE application=%s AND environment_id=%s AND image=%s AND state IN ('deploying','succeeded')",
                (application, environment_id, image),
            ).fetchone()
        )
