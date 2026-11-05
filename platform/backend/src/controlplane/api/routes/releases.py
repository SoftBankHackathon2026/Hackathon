from typing import Annotated

from fastapi import APIRouter, Query

from controlplane.api.deps import DeployerIdentity, DeploymentsDep, OperatorIdentity, ViewerIdentity
from controlplane.domain.models import Plan
from controlplane.providers.interface import DeploymentSpec, DeploymentStatus
from controlplane.schemas.releases import ReleaseAction, RestoreRelease, SubmitRelease

router = APIRouter(prefix="/api/v1", tags=["releases"])


@router.post("/plans")
def plan(spec: DeploymentSpec, deployments: DeploymentsDep, identity: ViewerIdentity) -> Plan:
    return deployments.preview(spec, identity)


@router.post("/releases", status_code=202)
def submit(
    body: SubmitRelease, deployments: DeploymentsDep, identity: DeployerIdentity
) -> DeploymentStatus:
    return deployments.submit_planned(
        body.request, body.idempotency_key, body.plan_fingerprint, identity
    )


@router.get("/releases")
def releases(
    deployments: DeploymentsDep,
    identity: OperatorIdentity,
    environment_id: Annotated[str | None, Query(max_length=63)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DeploymentStatus]:
    return deployments.list_releases(
        identity, environment_id=environment_id, limit=limit, offset=offset
    )


@router.post("/releases/{release_id}/actions", status_code=202)
def action(
    release_id: str, body: ReleaseAction, deployments: DeploymentsDep, identity: DeployerIdentity
) -> DeploymentStatus:
    return deployments.request_action(release_id, body.action, identity)


@router.post("/releases/{release_id}/restore", status_code=202)
def restore(
    release_id: str, body: RestoreRelease, deployments: DeploymentsDep, identity: DeployerIdentity
) -> DeploymentStatus:
    return deployments.restore(release_id, body.idempotency_key, identity)


@router.get("/releases/{release_id}")
def detail(
    release_id: str, deployments: DeploymentsDep, identity: OperatorIdentity
) -> DeploymentStatus:
    return deployments.observe(release_id, identity)
