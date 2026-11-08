import re
from typing import Annotated

from fastapi import APIRouter, Query, Response

from controlplane.api.deps import (
    ContainerDep,
    CutoverCommandsDep,
    CutoverOperatorIdentity,
    DataMappingsDep,
    FleetDep,
    IngestCommandsDep,
    IngestDep,
    MigrationsDep,
    PodLogsDep,
    SecurityDep,
    TasksDep,
    ViewerIdentity,
)
from controlplane.core.config import AuthMode
from controlplane.core.errors import InvalidRequest, OperationNotFound
from controlplane.schemas.data_mapping import DataMapping, DataMappingUpdate
from controlplane.schemas.migrations import (
    ClaimedCommand,
    CommandCreate,
    CommandResult,
    CutoverCommand,
    Fleet,
    MigrationCreate,
    MigrationCreated,
    MigrationDetail,
    MigrationEvent,
    MigrationIngest,
    MigrationSeries,
    PodLogs,
    TaskPlan,
)
from controlplane.schemas.security import SecurityPosture
from controlplane.services.data_mapping import DataMappingService
from controlplane.services.registered_environments import registered_environments

router = APIRouter(prefix="/api/v1", tags=["migrations"])


@router.get("/fleet")
def fleet(service: FleetDep, identity: ViewerIdentity, container: ContainerDep) -> Fleet:
    view = service.get_fleet()
    known = {env.id for env in view.environments}
    for env in registered_environments(container.connections, container.auth_mode, identity):
        if env.id not in known:
            view.environments.append(env)
    return view


@router.get("/fleet/environments/{env_id}/components/{component_id}/pods/{pod}/logs")
def pod_logs(
    env_id: str,
    component_id: str,
    pod: str,
    service: PodLogsDep,
    identity: ViewerIdentity,
    response: Response,
    container: Annotated[str | None, Query(max_length=63)] = None,
    tail: Annotated[int, Query()] = 200,
    previous: bool = False,
) -> PodLogs:
    response.headers["Cache-Control"] = "no-store"
    return service.logs(
        env_id, component_id, pod, container=container, tail=tail, previous=previous
    )


@router.get("/migrations/{job_id}")
def migration(job_id: str, service: MigrationsDep, identity: ViewerIdentity) -> MigrationDetail:
    return service.get_detail(job_id)


@router.post("/migrations", status_code=201)
def create_migration(
    body: MigrationCreate,
    tasks: TasksDep,
    identity: CutoverOperatorIdentity,
    container: ContainerDep,
) -> MigrationCreated:
    providers = {}
    for side, env_id in (("source", body.source_env), ("target", body.target_env)):
        if not re.fullmatch(r"connected-[A-Za-z0-9_-]{22}", env_id):
            continue
        connection = container.connections
        if connection is None or (identity is None and container.auth_mode != AuthMode.DEMO):
            raise InvalidRequest("등록한 환경을 확인할 수 없어요.")
        team = identity.team if identity else connection.runtime_tenant_id
        provider = connection.registered_provider(env_id, team)
        if provider is None:
            raise InvalidRequest("등록한 환경을 확인할 수 없어요.")
        providers[f"{side}_provider"] = provider
    return tasks.create(body.model_copy(update=providers), identity.email if identity else None)


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


@router.post("/ingest/commands/claim", response_model=ClaimedCommand | None)
def claim_any_command(service: IngestCommandsDep):
    claimed = service.claim_any()
    if claimed is None:
        return Response(status_code=204)
    return claimed


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
