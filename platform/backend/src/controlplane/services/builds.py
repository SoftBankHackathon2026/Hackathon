from typing import Any

from controlplane.builds.logs import CodeBuildLogReader
from controlplane.builds.models import BuildInput, BuildSettings
from controlplane.builds.store import BuildStore
from controlplane.core.errors import (
    InvalidRequest,
    NotConfigured,
    OperationNotFound,
    ProviderReadFailed,
    RequestConflict,
)
from controlplane.repositories.releases import ReleaseStore
from controlplane.schemas.accounts import Identity
from controlplane.schemas.builds import BuildConfig, BuildLogs, BuildView
from controlplane.services.deployments import DeploymentService

PLAN_CHANGED = "설정이 변경됐습니다. 배포 계획을 다시 확인하세요."


def build_view(row: dict[str, Any]) -> BuildView:
    return BuildView(
        id=row["id"],
        application=row["application"],
        environment_id=row["environment_id"],
        state=row["state"],
        image=row["image"],
        release_id=row["release_id"],
        error_code=row["error_code"],
        events=row["events"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        repository=row["request"]["repository"],
        revision=row["request"]["revision"],
        commit=row["checkpoint"].get("commit"),
        build_id=row["checkpoint"].get("build_id"),
    )


class BuildService:
    def __init__(
        self,
        settings: BuildSettings | None,
        releases: ReleaseStore | None,
        deployments: DeploymentService,
    ) -> None:
        self.settings = settings
        self.deployments = deployments
        self.store = BuildStore(releases.dsn) if releases else None

    def config(self) -> BuildConfig:
        return BuildConfig(
            enabled=self.settings is not None,
            repositories=self.settings.repositories if self.settings else [],
            required_databases=self.settings.required_databases if self.settings else {},
        )

    def _enabled(self) -> tuple[BuildSettings, BuildStore]:
        if self.settings is None or self.store is None:
            raise NotConfigured("소스 빌드 Worker가 아직 연결되지 않았습니다.")
        return self.settings, self.store

    def _owned(self, build_id: str, identity: Identity | None) -> dict[str, Any]:
        _, store = self._enabled()
        row = store.get(build_id)
        if (
            not row
            or not self.deployments.registry.get(row["environment_id"])
            or (identity and row.get("team") != identity.team)
        ):
            raise OperationNotFound("빌드를 찾을 수 없습니다.")
        return row

    def _check_input(self, settings: BuildSettings, request: BuildInput) -> None:
        if request.deployment.architecture != "X86_64":
            raise InvalidRequest("현재 빌드는 linux/amd64를 지원합니다.")
        required = settings.required_databases.get(request.repository)
        resources = request.deployment.resources
        if required and (resources is None or resources.database != required):
            raise InvalidRequest(
                "이 저장소의 앱에는 PostgreSQL이 필요합니다. 데이터베이스 설정을 선택하세요."
            )

    def _check_target(self, request: BuildInput, identity: Identity | None) -> None:
        plan = self.deployments.plan(request.deployment, identity)
        if request.plan_fingerprint and plan.fingerprint != request.plan_fingerprint:
            raise RequestConflict(PLAN_CHANGED)
        if identity:
            owner = self.deployments.accounts.owners().get(request.deployment.environment_id)
            if owner not in (None, identity.team):
                raise RequestConflict("다른 팀이 사용 중인 환경입니다.")

    def submit(self, request: BuildInput, identity: Identity | None) -> BuildView:
        settings, store = self._enabled()
        self._check_input(settings, request)
        try:
            self._check_target(request, identity)
            row = store.enqueue(
                request,
                settings.max_pending,
                settings.max_daily,
                team=identity.team if identity else None,
                created_by=identity.email if identity else None,
            )
        except RequestConflict:
            raise
        except ValueError:
            raise InvalidRequest("배포 대상·자원·DB 설정을 먼저 확인하세요.") from None
        return build_view(row)

    def logs(self, build_id: str, identity: Identity | None) -> BuildLogs:
        settings, _ = self._enabled()
        handle = self._owned(build_id, identity)["checkpoint"].get("build_id")
        if not handle:
            return BuildLogs(lines=[])
        try:
            return BuildLogs(lines=CodeBuildLogReader(settings).read(handle))
        except Exception:
            raise ProviderReadFailed("빌드 로그를 읽지 못했습니다.") from None

    def reconcile(self, build_id: str, identity: Identity | None) -> BuildView:
        _, store = self._enabled()
        self._owned(build_id, identity)
        return build_view(store.reconcile(build_id))

    def history(
        self, identity: Identity | None, *, environment_id: str | None, limit: int, offset: int
    ) -> list[BuildView]:
        _, store = self._enabled()
        rows = store.list(
            team=identity.team if identity else None,
            environment_id=environment_id,
            limit=limit,
            offset=offset,
        )
        return [
            build_view(row) for row in rows if self.deployments.registry.get(row["environment_id"])
        ]
