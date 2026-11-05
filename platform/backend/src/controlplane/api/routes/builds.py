from typing import Annotated

from fastapi import APIRouter, Depends, Query

from controlplane.api.deps import BuildsDep, DeployerIdentity, OperatorIdentity, require_operator
from controlplane.builds.models import BuildInput
from controlplane.schemas.builds import BuildConfig, BuildLogs, BuildView

router = APIRouter(
    prefix="/api/v1/builds", tags=["builds"], dependencies=[Depends(require_operator)]
)


@router.get("/config")
def config(builds: BuildsDep) -> BuildConfig:
    return builds.config()


@router.post("", status_code=202)
def submit(request: BuildInput, builds: BuildsDep, identity: DeployerIdentity) -> BuildView:
    return builds.submit(request, identity)


@router.get("/{build_id}/logs")
def logs(build_id: str, builds: BuildsDep, identity: OperatorIdentity) -> BuildLogs:
    return builds.logs(build_id, identity)


@router.post("/{build_id}/reconcile")
def reconcile(build_id: str, builds: BuildsDep, identity: DeployerIdentity) -> BuildView:
    return builds.reconcile(build_id, identity)


@router.get("")
def history(
    builds: BuildsDep,
    identity: OperatorIdentity,
    environment_id: Annotated[str | None, Query(max_length=63)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[BuildView]:
    return builds.history(identity, environment_id=environment_id, limit=limit, offset=offset)
