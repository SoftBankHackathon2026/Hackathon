from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from controlplane.core.errors import OperationNotFound, RequestConflict
from controlplane.repositories.agent import DECISION_PENDING, RERUN_PENDING, AgentJournal
from controlplane.schemas.accounts import Identity
from controlplane.schemas.agent import (
    RERUN_STATES,
    WAITING_APPROVAL,
    AgentCommand,
    AgentHeartbeat,
    AgentStatus,
    AgentStatusList,
    ApprovalTicket,
    ChangeSummary,
    CommandAccepted,
    CommandKind,
    Decision,
    DecisionAccepted,
    PendingApproval,
    RunDetail,
    RunList,
    RunListItem,
    RunReport,
    RunSummary,
    SettingsChange,
    StoredRun,
)

RUN_NOT_FOUND = "해당 에이전트 실행 기록이 없어요."
APPROVAL_NOT_FOUND = "해당 승인 요청이 없어요."
AGENT_NOT_FOUND = "신호를 보낸 적 없는 에이전트예요."
COMMAND_NOT_FOUND = "해당 에이전트 명령이 없어요."
NOT_WAITING = "승인을 기다리는 실행이 아니어서 결정할 수 없어요."
SCREEN_REQUESTER = "운영 화면"
ONLINE_FACTOR = 3
ONLINE_MIN_SECONDS = 90


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def summarize(detail: dict[str, Any]) -> RunSummary:
    codes = _mapping(detail.get("pre_decision")).get("reason_codes")
    changes = _mapping(detail.get("proposal")).get("changes")
    diagnosis = _mapping(detail.get("diagnosis")).get("detail")
    return RunSummary(
        reason_codes=[str(code) for code in codes] if isinstance(codes, list) else [],
        changes=[
            ChangeSummary(key=str(change["key"]), value=str(change["value"]))
            for change in (changes if isinstance(changes, list) else [])
            if isinstance(change, dict) and "key" in change and "value" in change
        ],
        diagnosis=diagnosis if isinstance(diagnosis, str) else None,
    )


def is_online(received_at: datetime, interval_seconds: int, now: datetime) -> bool:
    window = max(ONLINE_FACTOR * interval_seconds, ONLINE_MIN_SECONDS)
    return now - received_at <= timedelta(seconds=window)


def _item(run: StoredRun) -> RunListItem:
    return RunListItem(
        run_id=run.run_id,
        agent_id=run.agent_id,
        job_id=run.job_id,
        state=run.state,
        created_at=run.created_at,
        updated_at=run.updated_at,
        summary=summarize(run.detail),
    )


class AgentService:
    def __init__(
        self, journal: AgentJournal, now: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._journal = journal
        self._now = now

    def heartbeat(self, beat: AgentHeartbeat) -> None:
        self._journal.heartbeat(beat, self._now())

    def report(self, run_id: str, report: RunReport) -> None:
        self._journal.put_run(run_id, report)

    def approvals(self, agent_id: str) -> list[ApprovalTicket]:
        return self._journal.approvals(agent_id)

    def ack(self, approval_id: int, result_state: str) -> None:
        if not self._journal.ack(approval_id, result_state, self._now()):
            raise OperationNotFound(APPROVAL_NOT_FOUND)

    def status(self, permitted: bool) -> AgentStatusList:
        now = self._now()
        pending = self._journal.open_commands()
        return AgentStatusList(
            agents=[
                AgentStatus(
                    **row,
                    online=is_online(row["received_at"], row["interval_seconds"], now),
                    pending_commands=pending.get(row["agent_id"], []),
                )
                for row in self._journal.heartbeats()
            ],
            can_command=permitted,
        )

    def _rerun_pending(self, agent_id: str) -> bool:
        return any(
            command.kind == "rerun"
            for command in self._journal.open_commands(agent_id).get(agent_id, [])
        )

    def _command(
        self, agent_id: str, kind: CommandKind, payload: dict, identity: Identity | None
    ) -> CommandAccepted:
        if not self._journal.known_agent(agent_id):
            raise OperationNotFound(AGENT_NOT_FOUND)
        if kind == "rerun" and self._rerun_pending(agent_id):
            raise RequestConflict(RERUN_PENDING)
        command = self._journal.command(
            agent_id, kind, payload, identity.email if identity else SCREEN_REQUESTER, self._now()
        )
        return CommandAccepted(id=command.id, kind=command.kind)

    def change_settings(
        self, agent_id: str, change: SettingsChange, identity: Identity | None
    ) -> CommandAccepted:
        return self._command(agent_id, "settings", change.model_dump(exclude_unset=True), identity)

    def rerun(self, agent_id: str, identity: Identity | None) -> CommandAccepted:
        return self._command(agent_id, "rerun", {}, identity)

    def commands(self, agent_id: str) -> list[AgentCommand]:
        return self._journal.open_commands(agent_id).get(agent_id, [])

    def ack_command(self, command_id: int, result: str) -> None:
        if not self._journal.ack_command(command_id, result, self._now()):
            raise OperationNotFound(COMMAND_NOT_FOUND)

    def runs(self, limit: int) -> RunList:
        return RunList(runs=[_item(run) for run in self._journal.runs(limit)])

    def _run(self, run_id: str) -> StoredRun:
        run = self._journal.run(run_id)
        if run is None:
            raise OperationNotFound(RUN_NOT_FOUND)
        return run

    def detail(self, run_id: str, permitted: bool) -> RunDetail:
        run = self._run(run_id)
        pending = self._journal.pending(run_id)
        return RunDetail(
            **_item(run).model_dump(),
            detail=run.detail,
            events=run.events,
            pending_approval=PendingApproval(
                id=pending.id,
                decision=pending.decision,
                requested_by=pending.requested_by,
                requested_at=pending.requested_at,
            )
            if pending
            else None,
            can_decide=permitted and run.state == WAITING_APPROVAL and pending is None,
            can_rerun=permitted
            and run.state in RERUN_STATES
            and not self._rerun_pending(run.agent_id),
        )

    def decide(
        self, run_id: str, decision: Decision, identity: Identity | None
    ) -> DecisionAccepted:
        return self.decide_as(run_id, decision, identity.email if identity else SCREEN_REQUESTER)

    def decide_as(self, run_id: str, decision: Decision, requested_by: str) -> DecisionAccepted:
        run = self._run(run_id)
        if run.state != WAITING_APPROVAL:
            raise RequestConflict(NOT_WAITING)
        if self._journal.pending(run_id) is not None:
            raise RequestConflict(DECISION_PENDING)
        ticket = self._journal.decide(run_id, decision, requested_by, self._now())
        return DecisionAccepted(id=ticket.id, decision=ticket.decision)
