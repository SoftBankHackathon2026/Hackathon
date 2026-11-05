from typing import Annotated

from fastapi import APIRouter, Query

from controlplane.api.deps import MonitoringDep, OperatorIdentity
from controlplane.providers.interface import AppLogs
from controlplane.providers.telemetry import TraceDetail, TraceKind, TraceList, Traffic

router = APIRouter(prefix="/api/v1/environments/{environment_id}", tags=["monitoring"])


@router.get("/logs")
async def app_logs(
    environment_id: str,
    monitoring: MonitoringDep,
    identity: OperatorIdentity,
    minutes: Annotated[int, Query(ge=1, le=10080)] = 30,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> AppLogs:
    return await monitoring.logs(
        environment_id, identity, minutes=minutes, query=q or None, limit=limit
    )


@router.get("/traffic")
async def traffic(
    environment_id: str,
    monitoring: MonitoringDep,
    identity: OperatorIdentity,
    minutes: Annotated[int, Query(ge=5, le=360)] = 60,
) -> Traffic:
    return await monitoring.traffic(environment_id, identity, minutes=minutes)


@router.get("/traces")
async def traces(
    environment_id: str,
    monitoring: MonitoringDep,
    identity: OperatorIdentity,
    minutes: Annotated[int, Query(ge=1, le=360)] = 30,
    kind: TraceKind = "all",
) -> TraceList:
    return await monitoring.traces(environment_id, identity, minutes=minutes, kind=kind)


@router.get("/traces/{trace_id}")
async def trace(
    environment_id: str, trace_id: str, monitoring: MonitoringDep, identity: OperatorIdentity
) -> TraceDetail:
    return await monitoring.trace(environment_id, identity, trace_id)
