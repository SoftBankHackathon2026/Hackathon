from typing import Any

from psycopg import Connection
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from controlplane.core.errors import RequestConflict
from controlplane.core.locks import AdvisoryLock, xact_lock
from controlplane.repositories.releases import ReleaseStore

Row = dict[str, Any]

APP_TEAM = (
    "COALESCE(a.team,(SELECT et.team FROM app_environments e JOIN environment_teams et "
    "ON et.environment_id=e.id WHERE e.application_id=a.id LIMIT 1)) AS team"
)


class CatalogStore:
    def __init__(self, releases: ReleaseStore) -> None:
        self._releases = releases

    def connect(self) -> Connection:
        return self._releases.connect()

    def locked(self, conn: Connection) -> None:
        xact_lock(conn, AdvisoryLock.CATALOG)

    def list_targets(self) -> list[Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT id,name,runtime,creation_enabled FROM deployment_targets ORDER BY name"
            ).fetchall()

    def get_application(self, app_id: str) -> Row | None:
        with self.connect() as conn:
            return conn.execute(
                f"SELECT a.*, {APP_TEAM} FROM applications a WHERE a.id=%s", (app_id,)
            ).fetchone()

    def list_applications(self) -> list[Row]:
        with self.connect() as conn:
            apps = conn.execute(
                f"SELECT a.id,a.name,a.created_at,{APP_TEAM} FROM applications a ORDER BY a.created_at"
            ).fetchall()
            for app in apps:
                app["environments"] = self._environments(conn, app["id"])
        return apps

    def _environments(self, conn: Connection, app_id: str) -> list[Row]:
        return conn.execute(
            "SELECT e.id,e.target_id,t.name,t.runtime FROM app_environments e "
            "JOIN deployment_targets t ON t.id=e.target_id WHERE e.application_id=%s "
            "ORDER BY e.created_at",
            (app_id,),
        ).fetchall()

    def list_environments(self, app_id: str) -> list[Row]:
        with self.connect() as conn:
            return self._environments(conn, app_id)

    def get_application_by_key(self, conn: Connection, key: str) -> Row | None:
        return conn.execute("SELECT * FROM applications WHERE creation_key=%s", (key,)).fetchone()

    def insert_application(
        self,
        conn: Connection,
        app_id: str,
        name: str,
        *,
        team: str | None,
        created_by: str | None,
        key: str,
    ) -> None:
        try:
            conn.execute(
                "INSERT INTO applications(id,name,team,created_by,creation_key) VALUES(%s,%s,%s,%s,%s)",
                (app_id, name, team, created_by, key),
            )
        except UniqueViolation:
            raise RequestConflict("같은 이름의 앱이 이미 있습니다.") from None

    def get_binding(self, environment_id: str) -> Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT e.*,a.name AS application_name,a.team,t.provider_binding_id "
                "FROM app_environments e JOIN applications a ON a.id=e.application_id "
                "JOIN deployment_targets t ON t.id=e.target_id WHERE e.id=%s",
                (environment_id,),
            ).fetchone()

    def list_dynamic_bindings(
        self, *, target_id: str | None = None, provider_binding_id: str | None = None
    ) -> list[Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT e.id FROM app_environments e JOIN deployment_targets t ON t.id=e.target_id "
                "WHERE e.binding IS NOT NULL AND (%s::text IS NULL OR e.target_id=%s) "
                "AND (%s::text IS NULL OR t.provider_binding_id=%s) ORDER BY e.created_at",
                (target_id, target_id, provider_binding_id, provider_binding_id),
            ).fetchall()

    def get_attachment(self, conn: Connection, app_id: str, target_id: str) -> Row | None:
        return conn.execute(
            "SELECT id FROM app_environments WHERE application_id=%s AND target_id=%s",
            (app_id, target_id),
        ).fetchone()

    def get_target(self, conn: Connection, target_id: str) -> Row | None:
        return conn.execute("SELECT * FROM deployment_targets WHERE id=%s", (target_id,)).fetchone()

    def hostname_taken(self, conn: Connection, hostname: str) -> bool:
        return bool(
            conn.execute("SELECT 1 FROM app_environments WHERE hostname=%s", (hostname,)).fetchone()
        )

    def insert_environment(
        self,
        conn: Connection,
        env_id: str,
        app_id: str,
        target_id: str,
        *,
        runtime_application: str,
        hostname: str | None,
        binding: Row,
    ) -> None:
        conn.execute(
            "INSERT INTO app_environments(id,application_id,target_id,runtime_application,hostname,binding) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (env_id, app_id, target_id, runtime_application, hostname, Jsonb(binding)),
        )

    def latest_succeeded_request(self, application: str, environment_id: str) -> Row | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT request FROM releases WHERE application=%s AND environment_id=%s "
                "AND state='succeeded' ORDER BY created_at DESC LIMIT 1",
                (application, environment_id),
            ).fetchone()
        return row["request"] if row else None

    def environment_exists(self, conn: Connection, environment_id: str) -> bool:
        return bool(
            conn.execute("SELECT 1 FROM app_environments WHERE id=%s", (environment_id,)).fetchone()
        )

    def set_creation_enabled(
        self, conn: Connection, provider_binding_id: str, enabled: bool
    ) -> None:
        conn.execute(
            "UPDATE deployment_targets SET creation_enabled=%s WHERE provider_binding_id=%s",
            (enabled, provider_binding_id),
        )

    def claim_owner(self, conn: Connection, environment_id: str) -> str | None:
        row = conn.execute(
            "SELECT team FROM environment_teams WHERE environment_id=%s", (environment_id,)
        ).fetchone()
        return row["team"] if row else None

    def import_application(self, conn: Connection, app_id: str, name: str, team: str | None) -> str:
        conn.execute(
            "INSERT INTO applications(id,name,team) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
            (app_id, name, team),
        )
        return conn.execute(
            "SELECT id FROM applications WHERE name=%s AND team IS NOT DISTINCT FROM %s",
            (name, team),
        ).fetchone()["id"]

    def import_target(
        self,
        conn: Connection,
        target_id: str,
        label: str,
        runtime: str,
        provider_binding_id: str,
        creation_enabled: bool,
    ) -> None:
        conn.execute(
            "INSERT INTO deployment_targets(id,name,runtime,provider_binding_id,creation_enabled) "
            "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET creation_enabled=excluded.creation_enabled",
            (target_id, label, runtime, provider_binding_id, creation_enabled),
        )

    def import_environment(
        self,
        conn: Connection,
        env_id: str,
        app_id: str,
        target_id: str,
        runtime_application: str,
        hostname: str | None,
    ) -> None:
        conn.execute(
            "INSERT INTO app_environments(id,application_id,target_id,runtime_application,hostname) "
            "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING",
            (env_id, app_id, target_id, runtime_application, hostname),
        )
