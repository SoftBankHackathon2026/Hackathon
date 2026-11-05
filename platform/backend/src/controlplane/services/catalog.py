import hashlib
import secrets
from typing import Any

from controlplane.core.errors import RequestConflict
from controlplane.domain.models import Runtime
from controlplane.providers.bindings import create_binding, resolve_binding
from controlplane.providers.interface import DeploymentProvider
from controlplane.repositories.catalog import CatalogStore
from controlplane.repositories.releases import ReleaseStore
from controlplane.schemas.accounts import Identity
from controlplane.schemas.catalog import CreateApplication

Row = dict[str, Any]

TARGET_LABELS = {
    Runtime.AWS_ECS: "AWS",
    Runtime.GCP_CLOUD_RUN: "GCP",
    Runtime.ONPREM_HELM: "온프렘",
}
LEGACY_TARGET_LABEL = "온프렘 (이전 구성)"
SHARED_RUNTIMES = {Runtime.ONPREM_HELM, Runtime.AWS_ECS, Runtime.GCP_CLOUD_RUN}
RUNTIME_APP_PREFIX_LIMIT = {Runtime.AWS_ECS: 13, Runtime.GCP_CLOUD_RUN: 8}
DEFAULT_PREFIX_LIMIT = 16
PUBLIC_DOMAIN = "yubin.website"
HOSTNAME_SUFFIX_LENGTHS = range(4, 7)
TEMPLATE_KEYS = ("instrumentation", "tracing_enabled", "strategy", "rollout")


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def creation_disabled_reason(runtime: str) -> str:
    if runtime in SHARED_RUNTIMES:
        return "새 앱을 만들 수 있는 공유 배포 환경이 설정되지 않았습니다."
    return "새 앱 생성은 아직 지원하지 않습니다. 기존 앱을 선택하면 배포할 수 있습니다."


class ApplicationCatalog:
    def __init__(self, store: ReleaseStore, registry: Any) -> None:
        self.store = store
        self.registry = registry
        self._db = CatalogStore(store)

    def bootstrap(self) -> None:
        with self._db.connect() as conn:
            for key, adapter in self.registry.listed_bindings():
                self._import_binding(conn, key, adapter)

    def _import_binding(self, conn: Any, key: str, adapter: DeploymentProvider) -> None:
        target = getattr(adapter.settings, "catalog_target_id", None)
        if self._db.environment_exists(conn, key):
            self._db.set_creation_enabled(conn, key, bool(target))
            return
        team = self._db.claim_owner(conn, key)
        app_id = self._db.import_application(
            conn, "app-" + _digest(adapter.application, team or "")[:20], adapter.application, team
        )
        target_id = target or "target-" + key
        label = TARGET_LABELS.get(adapter.runtime, LEGACY_TARGET_LABEL)
        self._db.import_target(conn, target_id, label, adapter.runtime, key, bool(target))
        hostname = getattr(getattr(adapter.settings, "environment", None), "hostname", None)
        self._db.import_environment(conn, key, app_id, target_id, adapter.application, hostname)

    def targets(self) -> list[Row]:
        rows = self._db.list_targets()
        for row in rows:
            row["creation_disabled_reason"] = (
                None if row["creation_enabled"] else creation_disabled_reason(row["runtime"])
            )
        return rows

    def application(self, app_id: str, identity: Identity | None = None) -> Row:
        row = self._db.get_application(app_id)
        if not row or (identity and row["team"] != identity.team):
            raise RequestConflict("이 앱을 찾을 수 없거나 접근 권한이 없습니다.")
        return row

    def list_applications(self, identity: Identity | None = None) -> list[Row]:
        return [
            app
            for app in self._db.list_applications()
            if identity is None or app["team"] in (None, identity.team)
        ]

    def list_environments(self, app_id: str) -> list[Row]:
        return self._db.list_environments(app_id)

    def create(self, request: CreateApplication, identity: Identity | None = None) -> Row:
        team = identity.team if identity else None
        with self._db.connect() as conn:
            self._db.locked(conn)
            old = self._db.get_application_by_key(conn, request.idempotency_key)
            if old:
                if (old["name"], old["team"]) != (request.name, team):
                    raise RequestConflict("같은 요청 키에 다른 앱 정보를 사용할 수 없습니다.")
                return {"id": old["id"], "name": old["name"]}
            app_id = "app-" + secrets.token_hex(10)
            self._db.insert_application(
                conn,
                app_id,
                request.name,
                team=team,
                created_by=identity.email if identity else None,
                key=request.idempotency_key,
            )
        return {"id": app_id, "name": request.name}

    def binding(self, environment_id: str) -> Row | None:
        return self._db.get_binding(environment_id)

    def dynamic_bindings(
        self, *, target_id: str | None = None, provider_binding_id: str | None = None
    ) -> list[Row]:
        return self._db.list_dynamic_bindings(
            target_id=target_id, provider_binding_id=provider_binding_id
        )

    def require_owner(self, environment_id: str, identity: Identity | None) -> bool:
        row = self.binding(environment_id)
        if row and row["binding"] is not None:
            self.application(row["application_id"], identity)
            return True
        return False

    def _base_for_target(self, target: Row | None) -> DeploymentProvider:
        if not target or not target["creation_enabled"]:
            raise RequestConflict("이 환경의 새 앱 연결은 아직 지원하지 않습니다.")
        base = self.registry.base_binding(target["provider_binding_id"])
        if not base or not getattr(base.settings, "catalog_target_id", None):
            raise RequestConflict("배포 환경 설정을 확인하세요.")
        return base

    def _allocate_hostname(self, conn: Any, prefix: str, suffix: str) -> str:
        for length in HOSTNAME_SUFFIX_LENGTHS:
            hostname = f"{prefix}-{suffix[:length]}.{PUBLIC_DOMAIN}"
            if not self._db.hostname_taken(conn, hostname):
                return hostname
        raise RequestConflict("사용 가능한 짧은 주소가 없습니다. 다른 앱 이름을 사용하세요.")

    def attach(self, app_id: str, target_id: str, identity: Identity | None = None) -> Row:
        app = self.application(app_id, identity)
        with self._db.connect() as conn:
            self._db.locked(conn)
            existing = self._db.get_attachment(conn, app_id, target_id)
            if existing:
                return existing
            base = self._base_for_target(self._db.get_target(conn, target_id))
            suffix = _digest(app_id, target_id)[:10]
            limit = RUNTIME_APP_PREFIX_LIMIT.get(base.runtime, DEFAULT_PREFIX_LIMIT)
            prefix = app["name"][:limit].rstrip("-")
            env_id = "ae-" + suffix
            hostname = (
                self._allocate_hostname(conn, prefix, suffix)
                if base.runtime == Runtime.ONPREM_HELM
                else None
            )
            runtime_app = f"{prefix}-{suffix}"
            payload = create_binding(
                base, runtime_app, env_id, hostname=hostname, owner=secrets.token_hex(16)
            )
            resolve_binding(base, payload, self.store)
            self._db.insert_environment(
                conn,
                env_id,
                app_id,
                target_id,
                runtime_application=runtime_app,
                hostname=hostname,
                binding=payload,
            )
        return {"id": env_id}

    def resolve(self, environment_id: str) -> DeploymentProvider | None:
        row = self.binding(environment_id)
        if not row or row["binding"] is None:
            return None
        base = self.registry.base_binding(row["provider_binding_id"])
        return resolve_binding(base, row["binding"], self.store) if base else None

    def _request_template(self, env: Row) -> Row:
        template = dict(env.get("request_template") or {})
        saved = self._db.latest_succeeded_request(env["application"], env["id"])
        for key in TEMPLATE_KEYS:
            if saved and key in saved:
                template[key] = saved[key]
        if template.get("tracing_enabled") is not False and not template.get("instrumentation"):
            options = env["instrumentation_options"]
            template["instrumentation"] = "auto" if "auto" in options else "python"
        return template

    def decorate(self, env: Row) -> Row:
        if env.get("instrumentation_options"):
            env["request_template"] = self._request_template(env)
        binding = self.binding(env["id"])
        if binding:
            env.update(
                application_id=binding["application_id"],
                application_name=binding["application_name"],
                target_id=binding["target_id"],
            )
        return env
