from typing import Any

from fastapi import APIRouter, Depends

from controlplane.api.deps import DeployerIdentity, OperatorIdentity, RunsDep, require_operator
from controlplane.demo_deployments.models import DemoDeployInput, PreviewInput, RetryRun, StartRun

router = APIRouter(prefix="/api/v1/demo", tags=["demo"], dependencies=[Depends(require_operator)])


@router.post("/deployment-previews", status_code=201)
def preview(request: PreviewInput, runs: RunsDep, identity: DeployerIdentity) -> dict[str, Any]:
    return runs.preview(request, identity)


@router.post("/deployments", status_code=202)
def launch(request: DemoDeployInput, runs: RunsDep, identity: DeployerIdentity) -> dict[str, Any]:
    return runs.launch(request, identity)


@router.post("/deployment-runs", status_code=202)
def start(request: StartRun, runs: RunsDep, identity: DeployerIdentity) -> dict[str, Any]:
    return runs.start(request, identity)


@router.get("/deployment-runs/{run_id}")
def get(run_id: str, runs: RunsDep, identity: OperatorIdentity) -> dict[str, Any]:
    return runs.get(run_id, identity)


@router.post("/deployment-runs/{run_id}/retry", status_code=202)
def retry(
    run_id: str, request: RetryRun, runs: RunsDep, identity: DeployerIdentity
) -> dict[str, Any]:
    return runs.retry(run_id, request, identity)
