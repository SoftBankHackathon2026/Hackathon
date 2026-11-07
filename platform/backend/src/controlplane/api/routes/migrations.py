from typing import Annotated

from fastapi import APIRouter, Query, Response

from controlplane.api.deps import (
    CutoverCommandsDep,
    CutoverOperatorIdentity,
    DataMappingsDep,
    FleetDep,
    IngestCommandsDep,
    IngestDep,
    MigrationsDep,
    SecurityDep,
    ViewerIdentity,
)
from controlplane.core.errors import OperationNotFound
from controlplane.schemas.data_mapping import DataMapping, DataMappingUpdate
from controlplane.schemas.migrations import (
    CommandCreate,
    CommandResult,
    CutoverCommand,
    Fleet,
    MigrationDetail,
    MigrationEvent,
    MigrationIngest,
    MigrationSeries,
    TaskPlan,
)
from controlplane.schemas.security import SecurityPosture
from controlplane.services.data_mapping import DataMappingService

router = APIRouter(prefix="/api/v1", tags=["migrations"])


@router.get("/fleet")
def fleet(service: FleetDep, identity: ViewerIdentity) -> Fleet:
    return service.get_fleet()


@router.get("/migrations/{job_id}")
def migration(job_id: str, service: MigrationsDep, identity: ViewerIdentity) -> MigrationDetail:
    return service.get_detail(job_id)


@router.get("/migrations/{job_id}/plan")
def plan(
    job_id: str, service: MigrationsDep, mappings: DataMappingsDep, identity: ViewerIdentity
) -> TaskPlan:
    return service.get_plan(job_id, _mapping_status(mappings, job_id))


def _mapping_status(mappings: DataMappingService, job_id: str) -> str | None:
    try:
        return mappings.get(job_id).status
    except OperationNotFound:
        return None


@router.get("/migrations/{job_id}/series")
def series(
    job_id: str,
    service: MigrationsDep,
    identity: ViewerIdentity,
    minutes: Annotated[int, Query(ge=1, le=1440)] = 60,
) -> MigrationSeries:
    return service.get_series(job_id, minutes)


@router.get("/migrations/{job_id}/events")
def events(
    job_id: str, service: MigrationsDep, commands: CutoverCommandsDep, identity: ViewerIdentity
) -> list[MigrationEvent]:
    merged = service.list_events(job_id) + commands.events(job_id)
    return sorted(merged, key=lambda event: event.at, reverse=True)


# 전환 제어: 플랫폼은 명령을 저장만 하고, 러너 Pod 의 보고기가 가져가 실행한다(pull).
@router.post("/migrations/{job_id}/commands", status_code=202)
def request_command(
    job_id: str,
    body: CommandCreate,
    service: CutoverCommandsDep,
    identity: CutoverOperatorIdentity,
) -> CutoverCommand:
    return service.request(job_id, body, identity)


@router.get("/migrations/{job_id}/commands")
def list_commands(
    job_id: str, service: CutoverCommandsDep, identity: ViewerIdentity
) -> list[CutoverCommand]:
    return service.list_commands(job_id)


@router.get("/migrations/{job_id}/data-mapping")
def data_mapping(job_id: str, service: DataMappingsDep, identity: ViewerIdentity) -> DataMapping:
    return service.get(job_id)


@router.post("/migrations/{job_id}/data-mapping")
def confirm_data_mapping(
    job_id: str,
    body: DataMappingUpdate,
    service: DataMappingsDep,
    identity: CutoverOperatorIdentity,
) -> DataMapping:
    return service.update(job_id, body, identity)


@router.get("/security/posture")
def posture(service: SecurityDep, identity: ViewerIdentity) -> SecurityPosture:
    return service.get_posture()


@router.post("/ingest/migrations/{job_id}", status_code=202)
def ingest(job_id: str, body: MigrationIngest, service: IngestDep) -> None:
    service.ingest(job_id, body)


@router.post("/ingest/migrations/{job_id}/commands/claim", response_model=CutoverCommand | None)
def claim_command(job_id: str, service: IngestCommandsDep):
    command = service.claim(job_id)
    if command is None:
        return Response(status_code=204)
    return command


@router.post("/ingest/migrations/{job_id}/commands/{command_id}/result")
def command_result(
    job_id: str, command_id: str, body: CommandResult, service: IngestCommandsDep
) -> CutoverCommand:
    return service.finish(job_id, command_id, body)
