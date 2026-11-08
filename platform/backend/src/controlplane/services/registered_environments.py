import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from controlplane.core.config import AuthMode
from controlplane.schemas.accounts import Identity
from controlplane.schemas.migrations import (
    ClusterView,
    ComponentView,
    EnvironmentIssue,
    EnvironmentView,
)
from controlplane.services.registered_observation import (
    component_id,
    observed_cluster,
    observed_components,
)

LOG = logging.getLogger(__name__)

PROVIDERS = {"kubernetes": "onprem", "aws": "aws", "gcp": "gcp"}
CLOUD_KINDS = {"aws": "eks", "gcp": "gke"}
VERSION = "Kubernetes 버전"
NODES = "노드 수"
DISCONNECTED = EnvironmentIssue(
    severity="warning",
    title="보안 연결이 끊겼어요",
    detail="준비 화면에서 연결을 확인해 주세요.",
)


def _observed_at(value: Any) -> datetime | None:
    if type(value) is not int or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, UTC)


def _kind(target_kind: str, version: str | None) -> str:
    if target_kind in CLOUD_KINDS:
        return CLOUD_KINDS[target_kind]
    return "k3s" if version and "k3s" in version else "kubernetes"


def _components(workloads: Any) -> list[tuple[str, ComponentView]]:
    if not isinstance(workloads, dict):
        return []
    return [
        (
            item["kind"],
            ComponentView(
                id=component_id(item["namespace"], item["kind"], item["name"]),
                name=item["name"],
                layer="app",
                kind="app",
                managed=False,
                status="unknown",
                detail=f"{item['kind']}, 복제본 {item['replicas']}개 (등록 때 수집)",
                namespace=item["namespace"],
            ),
        )
        for item in workloads.get("items", [])
    ]


def registered_environment(snapshot: dict[str, Any]) -> EnvironmentView | None:
    if snapshot.get("device") != "APPROVED":
        return None
    env_id, target = snapshot.get("environmentId"), snapshot.get("target")
    collected = snapshot.get("collected") or []
    if not env_id or not isinstance(target, dict) or not collected:
        return None
    provider = PROVIDERS.get(target.get("kind", ""))
    if provider is None:
        return None
    facts = {fact["label"]: fact["value"] for fact in collected[0].get("facts", [])}
    version = facts.get(VERSION)
    nodes = facts.get(NODES, "")
    connected = snapshot.get("tunnel") == "READY"
    baseline = _components(snapshot.get("workloads"))
    components = [component for _, component in baseline]
    observed_at = _observed_at(snapshot.get("collectedAt"))
    cluster = ClusterView(version=version, node_count=int(nodes) if nodes.isdigit() else None)
    issues = [] if connected else [DISCONNECTED]
    observation_state = "registered" if provider == "onprem" else None
    observed_namespaces = None
    observation = snapshot.get("observation") if provider == "onprem" else None
    if observation:
        latest = observation["latest"]
        if latest:
            report = latest["report"]
            observed_at = _observed_at(latest["at"])
            cluster = observed_cluster(report, observed_at)
            components = observed_components(report, baseline)
            scopes = report["namespaces"]
            observed_namespaces = [scope["name"] for scope in scopes]
            partial = (
                report["nodes"] is None
                or observation["scopeLimited"]
                or any(
                    any(scope[key] is None for key in ("workloads", "pods", "replica_sets"))
                    for scope in scopes
                )
            )
            observation_state = (
                "stale" if not observation["fresh"] else "partial" if partial else "current"
            )
        else:
            observation_state = "unavailable"
        if connected and observation_state in ("stale", "unavailable"):
            issues.append(
                EnvironmentIssue(
                    severity="warning",
                    title="최신 클러스터 정보를 읽지 못했어요",
                    detail="단말에서 Kubernetes 접속 설정과 읽기 권한을 확인해 주세요.",
                )
            )
    return EnvironmentView(
        id=env_id,
        name=target.get("name") or env_id,
        kind=_kind(target["kind"], cluster.version),
        provider=provider,
        region=target.get("region") or "-",
        role="legacy" if target.get("role") == "source" else "target",
        connected=connected,
        observed_at=observed_at,
        components=components,
        issues=issues,
        cluster=cluster,
        observation_state=observation_state,
        observed_namespaces=observed_namespaces,
        registered=True,
        project=target.get("project"),
    )


def registered_environments(
    connections: Any, auth_mode: AuthMode, identity: Identity | None
) -> list[EnvironmentView]:
    if connections is None:
        return []
    if identity is not None:
        actor = SimpleNamespace(team=identity.team, email=identity.email, can_write=False)
    elif auth_mode == AuthMode.DEMO:
        actor = SimpleNamespace(team=connections.runtime_tenant_id, email="demo", can_write=False)
    else:
        return []
    try:
        snapshots = connections.list(actor, include_observations=True)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("registered environments unavailable: %s", type(exc).__name__)
        return []
    views = (registered_environment(snapshot) for snapshot in snapshots)
    return [view for view in views if view is not None]
