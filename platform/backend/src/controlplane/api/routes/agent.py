from typing import Annotated

from fastapi import APIRouter, Path, Query, Response

from controlplane.api.deps import (
    AgentDep,
    DeployerIdentity,
    DeployPermitted,
    IngestAgentDep,
    ViewerIdentity,
)
from controlplane.schemas.agent import (
    AGENT_ID_PATTERN,
    RUN_ID_PATTERN,
    AgentCommand,
    AgentHeartbeat,
    AgentStatusList,
    ApprovalAck,
    ApprovalTicket,
    CommandAccepted,
    CommandAck,
    DecisionAccepted,
    RunDetail,
    RunList,
    RunReport,
    SettingsChange,
)

router = APIRouter(prefix="/api/v1", tags=["agent"])

RunId = Annotated[str, Path(pattern=RUN_ID_PATTERN)]
ApprovalId = Annotated[int, Path(ge=1, le=2**63 - 1)]
CommandId = Annotated[int, Path(ge=1, le=2**63 - 1)]
AgentId = Annotated[str, Path(pattern=AGENT_ID_PATTERN)]


@router.post("/ingest/agent/heartbeat", status_code=204)
def heartbeat(body: AgentHeartbeat, service: IngestAgentDep) -> Response:
    service.heartbeat(body)
    return Response(status_code=204)


@router.put("/ingest/agent/runs/{run_id}", status_code=204)
def report_run(run_id: RunId, body: RunReport, service: IngestAgentDep) -> Response:
    service.report(run_id, body)
    return Response(status_code=204)


@router.get("/ingest/agent/approvals")
def approvals(
    service: IngestAgentDep,
    agent_id: Annotated[str, Query(pattern=AGENT_ID_PATTERN)],
) -> list[ApprovalTicket]:
    return service.approvals(agent_id)


@router.post("/ingest/agent/approvals/{approval_id}/ack", status_code=204)
def ack(approval_id: ApprovalId, body: ApprovalAck, service: IngestAgentDep) -> Response:
    service.ack(approval_id, body.result_state)
    return Response(status_code=204)


@router.get("/ingest/agent/commands")
def commands(
    service: IngestAgentDep,
    agent_id: Annotated[str, Query(pattern=AGENT_ID_PATTERN)],
) -> list[AgentCommand]:
    return service.commands(agent_id)


@router.post("/ingest/agent/commands/{command_id}/ack", status_code=204)
def ack_command(command_id: CommandId, body: CommandAck, service: IngestAgentDep) -> Response:
    service.ack_command(command_id, body.result)
    return Response(status_code=204)


@router.get("/agent/status")
def status(
    service: AgentDep, identity: ViewerIdentity, permitted: DeployPermitted
) -> AgentStatusList:
    return service.status(permitted)


@router.post("/agent/agents/{agent_id}/settings", status_code=202)
def change_settings(
    agent_id: AgentId, body: SettingsChange, service: AgentDep, identity: DeployerIdentity
) -> CommandAccepted:
    return service.change_settings(agent_id, body, identity)


@router.post("/agent/agents/{agent_id}/rerun", status_code=202)
def rerun(agent_id: AgentId, service: AgentDep, identity: DeployerIdentity) -> CommandAccepted:
    return service.rerun(agent_id, identity)


@router.get("/agent/runs")
def runs(
    service: AgentDep,
    identity: ViewerIdentity,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RunList:
    return service.runs(limit)


@router.get("/agent/runs/{run_id}")
def run(
    run_id: RunId, service: AgentDep, identity: ViewerIdentity, permitted: DeployPermitted
) -> RunDetail:
    return service.detail(run_id, permitted)


@router.post("/agent/runs/{run_id}/approve", status_code=202)
def approve(run_id: RunId, service: AgentDep, identity: DeployerIdentity) -> DecisionAccepted:
    return service.decide(run_id, "approve", identity)


@router.post("/agent/runs/{run_id}/reject", status_code=202)
def reject(run_id: RunId, service: AgentDep, identity: DeployerIdentity) -> DecisionAccepted:
    return service.decide(run_id, "reject", identity)
