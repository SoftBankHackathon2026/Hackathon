from datetime import UTC, datetime

from controlplane.domain.cluster import cluster_view
from controlplane.domain.fleet import EnvironmentSnapshot
from controlplane.domain.topology import environment_components
from controlplane.integrations.base import FleetSource
from controlplane.schemas.migrations import (
    ComponentView,
    EnvironmentIssue,
    EnvironmentView,
    Fleet,
)
from controlplane.services.migrations import MigrationService

READ_ACCESS_HINT = {
    "onprem": "플랫폼 읽기 계정(hackathon-user kubeconfig)이 필요해요.",
    "aws": "플랫폼 읽기 권한이 필요해요. EKS 안에서 실행하면 ServiceAccount RBAC, 밖이면 EKS access entry예요.",
    "gcp": "플랫폼 읽기 권한이 필요해요. GKE 에 deploy/reader 를 적용하고 그 토큰 kubeconfig 를 넣어요.",
}


UNREAD_TITLES = {
    401: "클러스터 인증에 실패했어요",
    403: "클러스터를 읽을 권한이 없어요",
    404: "읽을 네임스페이스가 없어요",
}


def _unread_issue(snapshot: EnvironmentSnapshot) -> EnvironmentIssue:
    status = snapshot.cluster.read_status
    detail = snapshot.config.name
    if status == 404:
        detail = f"{detail} · ns {snapshot.config.app_namespace}"
    elif status in (401, 403):
        detail = READ_ACCESS_HINT.get(snapshot.config.provider, READ_ACCESS_HINT["onprem"])
    return EnvironmentIssue(
        severity="critical",
        title=UNREAD_TITLES.get(status, "클러스터에 연결하지 못했어요"),
        detail=detail,
    )


def _nothing_read_from_cluster(
    snapshot: EnvironmentSnapshot, components: list[ComponentView]
) -> bool:
    if snapshot.cluster.inventory is not None:
        return False
    return all(view.status == "unknown" for view in components)


def environment_issues(
    snapshot: EnvironmentSnapshot, components: list[ComponentView]
) -> list[EnvironmentIssue]:
    if not snapshot.cluster.connected:
        return [_unread_issue(snapshot)]
    issues = [
        EnvironmentIssue(
            severity="critical" if c.status == "down" else "warning",
            title=f"{c.name} {'중단' if c.status == 'down' else '일부만 준비됨'}",
            detail=c.detail,
        )
        for c in components
        if c.status in ("down", "degraded")
    ]
    if _nothing_read_from_cluster(snapshot, components):
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
    components = environment_components(snapshot)
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
        cluster=cluster_view(cluster),
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
