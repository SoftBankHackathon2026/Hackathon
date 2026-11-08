import logging
import re
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

LOG = logging.getLogger(__name__)

PROVIDERS = {"kubernetes": "onprem", "aws": "aws", "gcp": "gcp"}
CLOUD_KINDS = {"aws": "eks", "gcp": "gke"}
VERSION = "Kubernetes 버전"
NODES = "노드 수"
DISCONNECTED = EnvironmentIssue(
    severity="warning",
    title="보안 연결이 끊겼어요",
    detail="등록할 때 수집한 정보만 보여요. 준비 화면에서 다시 연결하세요.",
)


def _observed_at(value: Any) -> datetime | None:
    if type(value) is not int or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, UTC)


def _kind(target_kind: str, version: str | None) -> str:
    if target_kind in CLOUD_KINDS:
        return CLOUD_KINDS[target_kind]
    return "k3s" if version and "k3s" in version else "kubernetes"


def _component_id(item: dict[str, Any]) -> str:
    raw = f"{item['namespace']}-{item['kind']}-{item['name']}".lower()
    return re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")


def _components(workloads: Any) -> list[ComponentView]:
    if not isinstance(workloads, dict):
        return []
    return [
        ComponentView(
            id=_component_id(item),
            name=item["name"],
            layer="app",
            kind="app",
            managed=False,
            status="unknown",
            detail=f"{item['kind']}, 복제본 {item['replicas']}개 (등록 때 수집)",
            namespace=item["namespace"],
        )
        for item in workloads.get("items", [])
    ]


def registered_environment(snapshot: dict[str, Any]) -> EnvironmentView | None:
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
    connected = snapshot.get("device") == "APPROVED" and snapshot.get("tunnel") == "READY"
    return EnvironmentView(
        id=env_id,
        name=target.get("name") or env_id,
        kind=_kind(target["kind"], version),
        provider=provider,
        region=target.get("region") or "-",
        role="legacy" if target.get("role") == "source" else "target",
        connected=connected,
        observed_at=_observed_at(snapshot.get("collectedAt")),
        components=_components(snapshot.get("workloads")),
        issues=[] if connected else [DISCONNECTED],
        cluster=ClusterView(version=version, node_count=int(nodes) if nodes.isdigit() else None),
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
        snapshots = connections.list(actor)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("registered environments unavailable: %s", type(exc).__name__)
        return []
    views = (registered_environment(snapshot) for snapshot in snapshots)
    return [view for view in views if view is not None]
