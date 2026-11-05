"""App registration and app-level state in the platform DB.

Infra Notion "앱 등록": app-id, container port, health check path, domain
<app-id>.<apps_domain>, listener rule priority. Plus what the user chose for the
app (DB·캐시 사용 여부), their provisioning states, and the app's secret references.

Own tables next to the team's `releases` journal; team migrations are untouched.
Secret references live here instead of runtime.json so that adding one never
changes the binding fingerprint of a release that is already running.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from psycopg import Connection

from controlplane.core import db
from controlplane.core.locks import AdvisoryLock, xact_lock
from oneport_aws.execution.oneport_ecs_app_model import (
    RESOURCE_COLUMNS,
    DatabaseChoice,
    DatabaseMode,
    RegisteredApp,
    Registration,
    ResourceKind,
)
from oneport_aws.execution.oneport_ecs_schema import SCHEMA, AwsLock, aws_xact_lock
from oneport_aws.providers.oneport_ecs.models import OneportEcsEnvironment
from oneport_aws.providers.oneport_ecs.provisioning import IN_PROGRESS, TERMINAL, ResourceState
from oneport_aws.providers.oneport_ecs.routing import first_free_priority

APP_COLUMNS = "app_id, environment_id, port, health_path, domain, rule_priority"


class AppRegistry:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self):
        return db.connect(self.dsn)

    def migrate(self) -> None:
        with self.connect() as conn:
            aws_xact_lock(conn, AwsLock.SCHEMA)
            conn.execute(SCHEMA)

    def get(self, app_id: str, environment_id: str) -> RegisteredApp | None:
        with self.connect() as conn:
            return self._select(conn, app_id, environment_id)

    @staticmethod
    def _select(conn: Connection, app_id: str, environment_id: str) -> RegisteredApp | None:
        row = conn.execute(
            "SELECT * FROM oneport_apps WHERE app_id=%s AND environment_id=%s",
            (app_id, environment_id),
        ).fetchone()
        return RegisteredApp.from_row(row) if row else None

    def register(
        self,
        env: OneportEcsEnvironment,
        *,
        port: int,
        health_path: str,
        database: DatabaseChoice = "none",
        database_mode: DatabaseMode = "dedicated",
        shared_ref: str | None = None,
        redis: bool | None = None,
        taken: set[int] = frozenset(),
    ) -> RegisteredApp:
        registration = Registration(port, health_path, database, database_mode, shared_ref, redis)
        registration.validate(env)
        with self.connect() as conn:
            self._lock_for_registration(conn, env)
            existing = self._select(conn, env.application, env.environment_id)
            if (
                existing
                and existing.database != "none"
                and existing.database_mode != registration.database_mode
            ):
                raise ValueError("DB 모드 변경에는 별도 데이터 이전이 필요합니다.")
            if registration.wants_database and registration.database_mode == "shared":
                self.put_ref(conn, env.application, env.environment_id, "db", shared_ref)
            wants_cache, cache_state = self._cache_choice(existing, registration)
            if existing:
                return self._update(conn, env, existing, registration, wants_cache, cache_state)
            return self._insert(conn, env, registration, taken, wants_cache, cache_state)

    @staticmethod
    def _lock_for_registration(conn: Connection, env: OneportEcsEnvironment) -> None:
        for kind in ResourceKind:
            acquired = conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(current_schema()), hashtext(%s)) AS acquired",
                (f"{kind}:{env.application}:{env.environment_id}",),
            ).fetchone()["acquired"]
            if not acquired:
                raise ValueError("DB·캐시 작업 진행 중입니다. 완료 후 앱 설정을 변경하세요.")
        aws_xact_lock(conn, AwsLock.PRIORITY)
        xact_lock(conn, AdvisoryLock.RELEASE_INTAKE)
        active = conn.execute(
            "SELECT 1 FROM releases WHERE application=%s AND environment_id=%s AND state NOT IN ('succeeded', 'failed')",
            (env.application, env.environment_id),
        ).fetchone()
        if active:
            raise ValueError(
                "진행 중이거나 확인이 필요한 배포가 있어 앱 설정을 변경할 수 없습니다."
            )

    @staticmethod
    def _cache_choice(
        existing: RegisteredApp | None, registration: Registration
    ) -> tuple[bool, ResourceState]:
        cache_state = existing.cache_state if existing else ResourceState.NONE
        if existing and registration.redis is None:
            wants_cache = existing.redis
        else:
            wants_cache = bool(registration.redis)
        if wants_cache and cache_state == ResourceState.NONE:
            cache_state = ResourceState.REQUESTED
        if not wants_cache and cache_state != ResourceState.NONE:
            raise ValueError("캐시를 끄려면 cache-delete로 먼저 삭제하세요.")
        return wants_cache, cache_state

    def _update(
        self,
        conn: Connection,
        env: OneportEcsEnvironment,
        existing: RegisteredApp,
        registration: Registration,
        wants_cache: bool,
        cache_state: ResourceState,
    ) -> RegisteredApp:
        db_state = existing.db_state
        if registration.wants_database and db_state == ResourceState.NONE:
            db_state = ResourceState.REQUESTED
        if not registration.wants_database and (db_state in IN_PROGRESS or db_state in TERMINAL):
            raise ValueError("DB 생성 중에는 DB 선택을 끌 수 없습니다.")
        if registration.wants_database and registration.database_mode == "shared":
            db_state = ResourceState.READY
        row = conn.execute(
            """UPDATE oneport_apps SET port=%s, health_path=%s, domain=%s, database=%s,
                 db_state=%s, db_error=CASE WHEN %s='requested' THEN NULL ELSE db_error END,
                 db_updated_at=CASE WHEN %s<>db_state THEN now() ELSE db_updated_at END,
                 updated_at=now(), database_mode=%s, redis=%s, cache_state=%s,
                 cache_error=CASE WHEN %s='requested' THEN NULL ELSE cache_error END,
                 cache_updated_at=CASE WHEN %s<>cache_state THEN now() ELSE cache_updated_at END
               WHERE app_id=%s AND environment_id=%s RETURNING *""",
            (
                registration.port,
                registration.health_path,
                env.host,
                registration.database,
                db_state,
                db_state,
                db_state,
                registration.database_mode,
                wants_cache,
                cache_state,
                cache_state,
                cache_state,
                env.application,
                env.environment_id,
            ),
        ).fetchone()
        return RegisteredApp.from_row(row)

    def _insert(
        self,
        conn: Connection,
        env: OneportEcsEnvironment,
        registration: Registration,
        taken: set[int],
        wants_cache: bool,
        cache_state: ResourceState,
    ) -> RegisteredApp:
        used = self._used_priorities(conn, env.environment_id) | set(taken)
        priority = first_free_priority(env.first_rule_priority, used)
        db_state = ResourceState.NONE
        if registration.wants_database:
            shared = registration.database_mode == "shared"
            db_state = ResourceState.READY if shared else ResourceState.REQUESTED
        row = conn.execute(
            """INSERT INTO oneport_apps(app_id, environment_id, port, health_path, domain, rule_priority,
                                        database, db_state, database_mode, redis, cache_state)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                env.application,
                env.environment_id,
                registration.port,
                registration.health_path,
                env.host,
                priority,
                registration.database,
                db_state,
                registration.database_mode,
                wants_cache,
                cache_state,
            ),
        ).fetchone()
        return RegisteredApp.from_row(row)

    @staticmethod
    def _used_priorities(conn: Connection, environment_id: str) -> set[int]:
        rows = conn.execute(
            "SELECT rule_priority FROM oneport_apps WHERE environment_id=%s", (environment_id,)
        ).fetchall()
        return {row["rule_priority"] for row in rows}

    def reserve_priority(self, env: OneportEcsEnvironment, taken: set[int]) -> int:
        with self.connect() as conn:
            aws_xact_lock(conn, AwsLock.PRIORITY)
            row = conn.execute(
                "SELECT rule_priority FROM oneport_apps WHERE app_id=%s AND environment_id=%s FOR UPDATE",
                (env.application, env.environment_id),
            ).fetchone()
            if row is None:
                raise ValueError("등록되지 않은 앱입니다.")
            if row["rule_priority"] not in taken:
                return row["rule_priority"]
            used = set(taken) | self._used_priorities(conn, env.environment_id)
            priority = first_free_priority(env.first_rule_priority, used)
            conn.execute(
                "UPDATE oneport_apps SET rule_priority=%s, updated_at=now() WHERE app_id=%s AND environment_id=%s",
                (priority, env.application, env.environment_id),
            )
            return priority

    def unregister(self, app_id: str, environment_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM oneport_app_secrets WHERE app_id=%s AND environment_id=%s",
                (app_id, environment_id),
            )
            conn.execute(
                "DELETE FROM oneport_apps WHERE app_id=%s AND environment_id=%s",
                (app_id, environment_id),
            )

    @contextmanager
    def locked(
        self, app_id: str, environment_id: str, kind: ResourceKind = ResourceKind.DB
    ) -> Iterator[tuple[Connection, RegisteredApp | None]]:
        updated = RESOURCE_COLUMNS[kind].updated_at
        with self.connect() as conn:
            acquired = conn.execute(
                "SELECT pg_try_advisory_lock(hashtext(current_schema()), hashtext(%s)) AS acquired",
                (f"{kind}:{app_id}:{environment_id}",),
            ).fetchone()["acquired"]
            if not acquired:
                yield conn, None
                return
            row = conn.execute(
                f"""SELECT *, extract(epoch FROM now() - {updated}) AS age_seconds
                   FROM oneport_apps WHERE app_id=%s AND environment_id=%s FOR UPDATE SKIP LOCKED""",
                (app_id, environment_id),
            ).fetchone()
            yield conn, RegisteredApp.from_row(row) if row else None

    @staticmethod
    def set_state(
        conn: Connection,
        kind: ResourceKind,
        app_id: str,
        environment_id: str,
        state: ResourceState,
        error: str | None = None,
    ) -> None:
        columns = RESOURCE_COLUMNS[kind]
        conn.execute(
            f"""UPDATE oneport_apps SET {columns.state}=%s, {columns.error}=%s, {columns.updated_at}=now(), updated_at=now()
               WHERE app_id=%s AND environment_id=%s""",
            (state, error, app_id, environment_id),
        )

    @classmethod
    def set_db(
        cls,
        conn: Connection,
        app_id: str,
        environment_id: str,
        state: ResourceState,
        error: str | None = None,
    ) -> None:
        cls.set_state(conn, ResourceKind.DB, app_id, environment_id, state, error)

    def reset_db(self, app_id: str, environment_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE oneport_apps SET database='none', db_state='none', db_error=NULL,
                     db_updated_at=now(), updated_at=now() WHERE app_id=%s AND environment_id=%s""",
                (app_id, environment_id),
            )
            self._delete_ref(conn, app_id, environment_id, "db")

    def reset_cache(self, app_id: str, environment_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE oneport_apps SET redis=false, cache_state='none', cache_error=NULL,
                     cache_updated_at=now(), updated_at=now() WHERE app_id=%s AND environment_id=%s""",
                (app_id, environment_id),
            )
            self._delete_ref(conn, app_id, environment_id, "cache")

    @staticmethod
    def _delete_ref(conn: Connection, app_id: str, environment_id: str, binding: str) -> None:
        conn.execute(
            "DELETE FROM oneport_app_secrets WHERE app_id=%s AND environment_id=%s AND binding=%s",
            (app_id, environment_id, binding),
        )

    @staticmethod
    def put_ref(conn: Connection, app_id: str, environment_id: str, binding: str, ref: str) -> None:
        conn.execute(
            """INSERT INTO oneport_app_secrets(app_id, environment_id, binding, ref) VALUES (%s,%s,%s,%s)
               ON CONFLICT (app_id, environment_id, binding) DO UPDATE SET ref=EXCLUDED.ref, updated_at=now()""",
            (app_id, environment_id, binding, ref),
        )

    def set_ref(self, app_id: str, environment_id: str, binding: str, ref: str) -> None:
        with self.connect() as conn:
            self.put_ref(conn, app_id, environment_id, binding, ref)

    def refs(self, app_id: str, environment_id: str) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT binding, ref FROM oneport_app_secrets WHERE app_id=%s AND environment_id=%s",
                (app_id, environment_id),
            ).fetchall()
        return {r["binding"]: r["ref"] for r in rows}

    def set_restore(self, release_id: str, restore_of: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO oneport_restores(release_id, restore_of) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (release_id, restore_of),
            )

    def restore_of(self, release_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT restore_of FROM oneport_restores WHERE release_id=%s", (release_id,)
            ).fetchone()
        return row["restore_of"] if row else None
