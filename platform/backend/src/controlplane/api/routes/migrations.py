from typing import Annotated

from fastapi import APIRouter, Query

from controlplane.api.deps import FleetDep, IngestDep, MigrationsDep, SecurityDep, ViewerIdentity
from controlplane.schemas.migrations import (
    Fleet,
    MigrationDetail,
    MigrationEvent,
    MigrationIngest,
    MigrationSeries,
)
from controlplane.schemas.security import SecurityPosture

router = APIRouter(prefix="/api/v1", tags=["migrations"])


@router.get("/fleet")
def fleet(service: FleetDep, identity: ViewerIdentity) -> Fleet:
    return service.get_fleet()


@router.get("/migrations/{job_id}")
def migration(job_id: str, service: MigrationsDep, identity: ViewerIdentity) -> MigrationDetail:
    return service.get_detail(job_id)


@router.get("/migrations/{job_id}/series")
def series(
    job_id: str,
    service: MigrationsDep,
    identity: ViewerIdentity,
    minutes: Annotated[int, Query(ge=1, le=1440)] = 60,
) -> MigrationSeries:
    return service.get_series(job_id, minutes)


@router.get("/migrations/{job_id}/events")
def events(job_id: str, service: MigrationsDep, identity: ViewerIdentity) -> list[MigrationEvent]:
    return service.list_events(job_id)


@router.get("/security/posture")
def posture(service: SecurityDep, identity: ViewerIdentity) -> SecurityPosture:
    return service.get_posture()


@router.post("/ingest/migrations/{job_id}", status_code=202)
def ingest(job_id: str, body: MigrationIngest, service: IngestDep) -> None:
    service.ingest(job_id, body)
