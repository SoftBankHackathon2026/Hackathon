from datetime import UTC, datetime

from controlplane.domain.fleet import ClusterFacts, ComponentConfig, EnvironmentSnapshot
from controlplane.integrations.base import FleetSource
from controlplane.schemas.migrations import (
    AppState,
    ComponentView,
    EnvironmentIssue,
    EnvironmentView,
    Fleet,
)
from controlplane.services.migrations import MigrationService

APP_HEALTH_STATUS = {
    "Healthy": "healthy",
    "Progressing": "degraded",
    "Suspended": "degraded",
    "Degraded": "down",
    "Missing": "down",
}


def _workload_status(workload: str, cluster: ClusterFacts) -> tuple[str, str]:
    if not cluster.connected:
        return "unknown", "클러스터 미연결"
    if workload not in cluster.workload_ready:
        return "unknown", "상태를 읽지 못함"
    ready, desired = cluster.workload_ready[workload]
    if desired <= 0:
        return "down", "워크로드 없음"
    if ready >= desired:
        return "healthy", f"준비 {ready}/{desired}"
    return ("down" if ready == 0 else "degraded"), f"준비 {ready}/{desired}"


def _app_status(name: str, apps: list[AppState]) -> tuple[str, str]:
    app = next((a for a in apps if a.name == name), None)
    if app is None:
        return "unknown", "Argo CD 앱을 찾지 못함"
    return APP_HEALTH_STATUS.get(app.health, "unknown"), f"{app.sync} · {app.health}"


def component_view(component: ComponentConfig, cluster: ClusterFacts) -> ComponentView:
    if component.workload:
        status, detail = _workload_status(component.workload, cluster)
    elif component.argo_app:
        status, detail = _app_status(component.argo_app, cluster.apps)
    else:
        status, detail = "unknown", "관리형 서비스 상태는 아직 수집하지 않음"
    if component.note:
        detail = f"{detail} · {component.note}"
    return ComponentView(
        id=component.id,
        name=component.name,
        layer=component.layer,
        kind=component.kind,
        managed=component.managed,
        status=status,
        detail=detail,
        depends_on=component.depends_on,
        placement=component.placement,
    )


READ_ACCESS_HINT = {
    "onprem": "플랫폼 읽기 계정(hackathon-user kubeconfig)이 필요해요.",
    "aws": "플랫폼 읽기 권한이 필요해요. EKS 안에서 실행하면 ServiceAccount RBAC, 밖이면 EKS access entry예요.",
    "gcp": "플랫폼 읽기 권한이 필요해요. GKE RBAC와 Workload Identity 연동 kubeconfig예요.",
}


def environment_issues(
    snapshot: EnvironmentSnapshot, components: list[ComponentView]
) -> list[EnvironmentIssue]:
    if not snapshot.cluster.connected:
        return [
            EnvironmentIssue(
                severity="critical",
                title="클러스터에 연결하지 못했어요",
                detail=snapshot.config.name,
            )
        ]
    issues = [
        EnvironmentIssue(
            severity="critical" if c.status == "down" else "warning",
            title=f"{c.name} {'중단' if c.status == 'down' else '일부만 준비됨'}",
            detail=c.detail,
        )
        for c in components
        if c.status in ("down", "degraded")
    ]
    observable = [c for c in snapshot.config.components if c.workload or c.argo_app]
    if observable and all(
        view.status == "unknown" for view in components if view.id in {c.id for c in observable}
    ):
        issues.append(
            EnvironmentIssue(
                severity="warning",
                title="클러스터 구성 상태를 읽지 못했어요",
                detail=READ_ACCESS_HINT.get(snapshot.config.provider, READ_ACCESS_HINT["onprem"]),
            )
        )
    return issues


def environment_view(snapshot: EnvironmentSnapshot) -> EnvironmentView:
    config, cluster = snapshot.config, snapshot.cluster
    components = [component_view(c, cluster) for c in config.components]
    return EnvironmentView(
        id=config.id,
        name=config.name,
        kind=config.kind,
        provider=config.provider,
        region=config.region,
        role=config.role,
        connected=cluster.connected,
        observed_at=cluster.observed_at,
        apps=cluster.apps,
        data_services=config.data_services,
        components=components,
        boundary=config.boundary,
        issues=environment_issues(snapshot, components),
    )


class FleetService:
    def __init__(self, source: FleetSource, migrations: MigrationService) -> None:
        self._source = source
        self._migrations = migrations

    def get_fleet(self) -> Fleet:
        return Fleet(
            generated_at=datetime.now(UTC),
            source=self._source.mode,
            environments=[environment_view(s) for s in self._source.list_environments()],
            migrations=self._migrations.list_summaries(),
        )
