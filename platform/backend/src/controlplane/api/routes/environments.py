from typing import Any

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from controlplane.api.deps import (
    DeployerIdentity,
    DeploymentsDep,
    EnvironmentsDep,
    MonitoringDep,
    OperatorIdentity,
    ViewerIdentity,
)
from controlplane.schemas.deletions import DeletionView
from controlplane.schemas.environments import AppDeletionRequest, ApplicationRegistration

router = APIRouter(prefix="/api/v1/environments", tags=["environments"])


@router.get("")
def environments(service: EnvironmentsDep, identity: ViewerIdentity) -> list[dict[str, Any]]:
    return service.overview(identity)


@router.post("/{environment_id}/application")
async def register_application(
    environment_id: str,
    body: ApplicationRegistration,
    deployments: DeploymentsDep,
    monitoring: MonitoringDep,
    identity: DeployerIdentity,
) -> dict[str, Any]:
    registered = await run_in_threadpool(
        deployments.register_application, environment_id, identity, **body.model_dump()
    )
    monitoring.forget_environment(environment_id)
    return registered


@router.post("/{environment_id}/deletion", status_code=202)
def delete_application(
    environment_id: str,
    body: AppDeletionRequest,
    service: EnvironmentsDep,
    identity: DeployerIdentity,
) -> DeletionView | None:
    return service.request_deletion(environment_id, body, identity)


@router.get("/{environment_id}/deletion")
def application_deletion(
    environment_id: str, service: EnvironmentsDep, identity: OperatorIdentity
) -> DeletionView | None:
    return service.deletion(environment_id, identity)
