import secrets
from collections.abc import Callable
from contextlib import contextmanager
from importlib.resources import files
from typing import Any

from psycopg.types.json import Jsonb

from controlplane.core.errors import OperationNotFound, RequestConflict
from controlplane.core.locks import AdvisoryLock, session_unlock, try_session_lock, xact_lock
from controlplane.demo_deployments.snapshots import RunSnapshot
from controlplane.demo_deployments.states import (
    ACTIVE_RUN_STATES,
    ACTIVE_VALUES,
    RELEASE_SETTLED,
    RunState,
    TargetState,
)
from controlplane.repositories.releases import ReleaseStore

LOCK = AdvisoryLock.DEMO_RUNS
ACTIVE = ACTIVE_RUN_STATES
MAX_UNCONSUMED_PREVIEWS = 100
RETRYABLE_RUN_STATES = (RunState.FAILED, RunState.PARTIAL)

Row = dict[str, Any]
Validate = Callable[[RunSnapshot, Any], None]


class RunNotFound(OperationNotFound):
    pass


def _team(identity) -> tuple[str | None, str | None]:
    return (identity.team, identity.email) if identity else (None, None)


class RunStore(ReleaseStore):
    def migrate(self):
        with self.connect() as conn:
            xact_lock(conn, LOCK)
            conn.execute(files("controlplane.demo_deployments").joinpath("schema.sql").read_text())

    @contextmanager
    def run_lock(self):
        with self.connect() as conn:
            acquired = try_session_lock(conn, LOCK)
            conn.commit()
            try:
                yield conn if acquired else None
            finally:
                if acquired:
                    session_unlock(conn, LOCK)
                    conn.commit()

    @staticmethod
    def busy(conn, environments, exclude=None):
        return conn.execute(
            """SELECT 1 FROM demo_deployment_runs r
            JOIN demo_deployment_run_targets t ON t.run_id=r.id
            WHERE t.environment_id=ANY(%s) AND r.state=ANY(%s)
            AND (%s::text IS NULL OR r.id<>%s) LIMIT 1""",
            (environments, ACTIVE_VALUES, exclude, exclude),
        ).fetchone()

    @staticmethod
    def owned(row, identity):
        if not row or (identity and row["team"] != identity.team):
            raise RunNotFound("실행 기록을 찾을 수 없습니다.")
        return row

    @staticmethod
    def latest_release(conn, environment_id: str) -> Row | None:
        return conn.execute(
            "SELECT id,state,created_at FROM releases WHERE environment_id=%s "
            "ORDER BY created_at DESC LIMIT 1",
            (environment_id,),
        ).fetchone()

    @staticmethod
    def release_by_key(conn, key: str) -> Row | None:
        return conn.execute("SELECT * FROM releases WHERE idempotency_key=%s", (key,)).fetchone()

    @staticmethod
    def has_succeeded_release(conn, runtime: str, application: str, environment_id: str) -> bool:
        return (
            conn.execute(
                "SELECT id FROM releases WHERE runtime=%s AND application=%s "
                "AND environment_id=%s AND state='succeeded' "
                "AND archived IS DISTINCT FROM 'all' LIMIT 1",
                (runtime, application, environment_id),
            ).fetchone()
            is not None
        )

    @staticmethod
    def release_in_flight(latest: Row | None) -> bool:
        return latest is not None and latest["state"] not in RELEASE_SETTLED

    @staticmethod
    def run_by_key(conn, key: str) -> Row | None:
        return conn.execute(
            "SELECT * FROM demo_deployment_runs WHERE idempotency_key=%s", (key,)
        ).fetchone()

    @staticmethod
    def _check_capacity(conn, max_pending):
        count = conn.execute(
            "SELECT count(*) AS n FROM demo_deployment_runs WHERE state=ANY(%s)",
            (ACTIVE_VALUES,),
        ).fetchone()["n"]
        if max_pending is not None and count >= max_pending:
            raise RequestConflict("배포 대기열이 가득 찼습니다.")

    def preview(self, snapshot: RunSnapshot, identity):
        team, email = _team(identity)
        with self.connect() as conn:
            xact_lock(conn, LOCK)
            conn.execute("""DELETE FROM demo_deployment_previews p WHERE expires_at < now()
                AND NOT EXISTS (SELECT 1 FROM demo_deployment_runs r WHERE r.preview_id=p.id)""")
            count = conn.execute("""SELECT count(*) AS n FROM demo_deployment_previews p
                WHERE NOT EXISTS (SELECT 1 FROM demo_deployment_runs r WHERE r.preview_id=p.id)""").fetchone()[
                "n"
            ]
            if count >= MAX_UNCONSUMED_PREVIEWS:
                raise RequestConflict("사전 확인 요청이 많습니다. 잠시 후 다시 시도하세요.")
            return conn.execute(
                """INSERT INTO demo_deployment_previews(id,team,created_by,snapshot)
                VALUES(%s,%s,%s,%s) RETURNING *""",
                (secrets.token_hex(12), team, email, Jsonb(snapshot.persisted())),
            ).fetchone()

    def get_preview(self, preview_id, identity):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT *, expires_at > now() AS valid FROM demo_deployment_previews WHERE id=%s",
                (preview_id,),
            ).fetchone()
        return self.owned(row, identity)

    def start(self, request, identity, max_pending, validate: Validate, *, direct_input=None):
        with self.connect() as conn:
            xact_lock(conn, LOCK)
            old = self.run_by_key(conn, request.idempotency_key)
            if old:
                return self._replay(old, request, identity, direct_input)
            preview = self.get_preview(request.preview_id, identity)
            if not preview["valid"]:
                raise RequestConflict("사전 확인이 만료됐습니다. 다시 확인하세요.")
            snapshot = RunSnapshot.model_validate(preview["snapshot"])
            if not snapshot.ready:
                raise RequestConflict("배포 불가 사유를 해결한 뒤 다시 확인하세요.")
            validate(snapshot, identity)
            if conn.execute(
                "SELECT 1 FROM demo_deployment_runs WHERE preview_id=%s", (request.preview_id,)
            ).fetchone():
                raise RequestConflict("이미 접수한 사전 확인입니다. 기존 실행을 조회하세요.")
            if self.busy(conn, [t.environment_id for t in snapshot.targets]):
                raise RequestConflict("시연 대상의 실행이 진행 중이거나 결과 확인이 필요합니다.")
            self._check_capacity(conn, max_pending)
            run_id = self._insert_run(conn, request, identity, preview["snapshot"], snapshot)
        return self.get(run_id, identity)

    def _replay(self, old, request, identity, direct_input):
        self.owned(old, identity)
        same_input = direct_input is not None and old["snapshot"].get("input") == direct_input
        if old["preview_id"] != request.preview_id and not same_input:
            raise RequestConflict("같은 요청 키에 다른 사전 확인을 사용할 수 없습니다.")
        return self.get(old["id"], identity)

    @staticmethod
    def _insert_run(conn, request, identity, raw_snapshot, snapshot: RunSnapshot) -> str:
        team, email = _team(identity)
        run_id = secrets.token_hex(12)
        conn.execute(
            """INSERT INTO demo_deployment_runs
            (id,preview_id,idempotency_key,team,created_by,snapshot)
            VALUES(%s,%s,%s,%s,%s,%s)""",
            (run_id, request.preview_id, request.idempotency_key, team, email, Jsonb(raw_snapshot)),
        )
        for target in snapshot.targets:
            conn.execute(
                """INSERT INTO demo_deployment_run_targets(run_id,runtime,environment_id,application,owner_team)
                VALUES(%s,%s,%s,%s,%s)""",
                (
                    run_id,
                    target.runtime,
                    target.spec["environment_id"],
                    target.spec["application"],
                    target.owner_team,
                ),
            )
        return run_id

    def get(self, run_id, identity=None):
        with self.connect() as conn:
            row = self.owned(
                conn.execute(
                    "SELECT * FROM demo_deployment_runs WHERE id=%s", (run_id,)
                ).fetchone(),
                identity,
            )
            row["targets"] = conn.execute(
                "SELECT * FROM demo_deployment_run_targets WHERE run_id=%s ORDER BY runtime",
                (run_id,),
            ).fetchall()
            for target in row["targets"]:
                target["attempts"] = conn.execute(
                    """SELECT attempt,release_id FROM demo_deployment_run_attempts
                    WHERE run_id=%s AND runtime=%s ORDER BY attempt""",
                    (run_id, target["runtime"]),
                ).fetchall()
        return row

    def active_run_ids(self, conn) -> list[str]:
        rows = conn.execute(
            "SELECT id FROM demo_deployment_runs WHERE state=ANY(%s) ORDER BY created_at",
            (ACTIVE_VALUES,),
        ).fetchall()
        conn.commit()
        return [row["id"] for row in rows]

    def retry(self, run_id, request, identity, validate: Validate, max_pending):
        with self.connect() as conn:
            xact_lock(conn, LOCK)
            row = self.get(run_id, identity)
            runtimes = sorted(request.runtimes)
            old = conn.execute(
                "SELECT * FROM demo_deployment_run_retries WHERE idempotency_key=%s",
                (request.idempotency_key,),
            ).fetchone()
            if old:
                if old["run_id"] != run_id or old["runtimes"] != runtimes:
                    raise RequestConflict("같은 요청 키에 다른 재시도를 사용할 수 없습니다.")
                return row
            targets = self._retryable_targets(row, runtimes)
            if self.busy(conn, [targets[r]["environment_id"] for r in runtimes], run_id):
                raise RequestConflict("시연 대상의 다른 실행이 진행 중입니다.")
            snapshot = RunSnapshot.model_validate(row["snapshot"])
            self._check_no_newer_release(conn, snapshot, targets, runtimes)
            self._check_capacity(conn, max_pending)
            validate(snapshot, identity)
            self._reset_targets(conn, run_id, request, runtimes)
        return self.get(run_id, identity)

    @staticmethod
    def _retryable_targets(row, runtimes) -> dict[str, Row]:
        if row["state"] not in RETRYABLE_RUN_STATES or not row["image"]:
            raise RequestConflict("이미지가 확정된 종료 실행의 실패 환경만 재시도할 수 있습니다.")
        targets = {t["runtime"]: t for t in row["targets"]}
        if any(targets[r]["state"] != TargetState.FAILED for r in runtimes):
            raise RequestConflict(
                "실패한 환경만 재시도할 수 있습니다. 불확실한 결과는 먼저 확인하세요."
            )
        return targets

    def _check_no_newer_release(self, conn, snapshot: RunSnapshot, targets, runtimes):
        for runtime in runtimes:
            target = targets[runtime]
            latest = self.latest_release(conn, target["environment_id"])
            known = {a["release_id"] for a in target["attempts"]}
            known.add(snapshot.target(runtime).last_release_id)
            if latest and latest["id"] not in known:
                raise RequestConflict("이후 배포 기록이 있습니다. 새 사전 확인으로 배포하세요.")

    @staticmethod
    def _reset_targets(conn, run_id, request, runtimes):
        conn.execute(
            "INSERT INTO demo_deployment_run_retries(idempotency_key,run_id,runtimes) VALUES(%s,%s,%s)",
            (request.idempotency_key, run_id, Jsonb(runtimes)),
        )
        conn.execute(
            """UPDATE demo_deployment_run_targets SET state='pending',attempt=attempt+1,
            release_id=NULL,error_code=NULL,service_url=NULL WHERE run_id=%s AND runtime=ANY(%s)""",
            (run_id, runtimes),
        )
        conn.execute(
            "UPDATE demo_deployment_runs SET state='deploying',error_code=NULL,updated_at=now() WHERE id=%s",
            (run_id,),
        )

    @staticmethod
    def update(conn, run_id, state, *, build_id=None, image=None, error_code=None):
        conn.execute(
            """UPDATE demo_deployment_runs SET state=%s,build_id=COALESCE(%s,build_id),
            image=COALESCE(%s,image),error_code=%s,updated_at=now() WHERE id=%s""",
            (str(state), build_id, image, error_code, run_id),
        )
        conn.commit()

    @staticmethod
    def target(conn, run_id, runtime, state, *, release_id=None, error_code=None, service_url=None):
        conn.execute(
            """UPDATE demo_deployment_run_targets SET state=%s,release_id=COALESCE(%s,release_id),
            error_code=%s,service_url=%s WHERE run_id=%s AND runtime=%s""",
            (str(state), release_id, error_code, service_url, run_id, runtime),
        )
        if release_id:
            conn.execute(
                """INSERT INTO demo_deployment_run_attempts(run_id,runtime,attempt,release_id)
                SELECT run_id,runtime,attempt,release_id FROM demo_deployment_run_targets
                WHERE run_id=%s AND runtime=%s ON CONFLICT DO NOTHING""",
                (run_id, runtime),
            )
        conn.execute("UPDATE demo_deployment_runs SET updated_at=now() WHERE id=%s", (run_id,))
        conn.commit()
