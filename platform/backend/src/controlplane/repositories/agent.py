from datetime import datetime

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from controlplane.core.errors import RequestConflict
from controlplane.repositories.releases import ReleaseStore
from controlplane.schemas.agent import (
    AgentCommand,
    AgentHeartbeat,
    ApprovalTicket,
    CommandKind,
    Decision,
    RunReport,
    StoredRun,
)

DECISION_PENDING = "이 실행에 아직 에이전트가 가져가지 않은 결정이 있어요."
RUN_COLUMNS = "run_id, agent_id, job_id, state, created_at, updated_at, detail, events"
APPROVAL_COLUMNS = "id, run_id, decision, requested_by, requested_at"
COMMAND_COLUMNS = "id, kind, payload, requested_by, requested_at"
RERUN_PENDING = "이 에이전트에 아직 처리하지 않은 다시 판정 요청이 있어요."


class AgentJournal:
    def __init__(self, releases: ReleaseStore) -> None:
        self._releases = releases

    def connect(self):
        return self._releases.connect()

    def heartbeat(self, beat: AgentHeartbeat, received_at: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO agent_heartbeats"
                "(agent_id, version, auto_apply, interval_seconds, observed_at, received_at, config) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(agent_id) DO UPDATE SET "
                "version=excluded.version, auto_apply=excluded.auto_apply, "
                "interval_seconds=excluded.interval_seconds, observed_at=excluded.observed_at, "
                "received_at=excluded.received_at, "
                "config=COALESCE(excluded.config, agent_heartbeats.config)",
                (
                    beat.agent_id,
                    beat.version,
                    beat.auto_apply,
                    beat.interval_seconds,
                    beat.observed_at,
                    received_at,
                    Jsonb(beat.config) if beat.config is not None else None,
                ),
            )

    def heartbeats(self) -> list[dict]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT agent_id, version, auto_apply, interval_seconds, observed_at, received_at, "
                "config FROM agent_heartbeats ORDER BY agent_id"
            ).fetchall()

    def known_agent(self, agent_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM agent_heartbeats WHERE agent_id=%s", (agent_id,)
            ).fetchone()
        return row is not None

    def command(
        self,
        agent_id: str,
        kind: CommandKind,
        payload: dict,
        requested_by: str,
        requested_at: datetime,
    ) -> AgentCommand:
        try:
            with self.connect() as conn:
                row = conn.execute(
                    "INSERT INTO agent_commands(agent_id, kind, payload, requested_by, requested_at) "
                    f"VALUES(%s,%s,%s,%s,%s) RETURNING {COMMAND_COLUMNS}",
                    (agent_id, kind, Jsonb(payload), requested_by, requested_at),
                ).fetchone()
        except UniqueViolation:
            raise RequestConflict(RERUN_PENDING) from None
        return AgentCommand.model_validate(row)

    def open_commands(self, agent_id: str | None = None) -> dict[str, list[AgentCommand]]:
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT agent_id, {COMMAND_COLUMNS} FROM agent_commands "
                "WHERE consumed_at IS NULL AND (%s::text IS NULL OR agent_id=%s) "
                "ORDER BY requested_at, id",
                (agent_id, agent_id),
            ).fetchall()
        grouped: dict[str, list[AgentCommand]] = {}
        for row in rows:
            grouped.setdefault(row["agent_id"], []).append(
                AgentCommand.model_validate({k: v for k, v in row.items() if k != "agent_id"})
            )
        return grouped

    def ack_command(self, command_id: int, result: str, consumed_at: datetime) -> bool:
        with self.connect() as conn:
            conn.execute(
                "UPDATE agent_commands SET consumed_at=%s, result=%s "
                "WHERE id=%s AND consumed_at IS NULL",
                (consumed_at, result, command_id),
            )
            row = conn.execute("SELECT 1 FROM agent_commands WHERE id=%s", (command_id,)).fetchone()
        return row is not None

    def put_run(self, run_id: str, report: RunReport) -> None:
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO agent_runs({RUN_COLUMNS}) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(run_id) DO UPDATE SET agent_id=excluded.agent_id, "
                "job_id=excluded.job_id, state=excluded.state, updated_at=excluded.updated_at, "
                "detail=excluded.detail, events=excluded.events "
                "WHERE agent_runs.updated_at <= excluded.updated_at",
                (
                    run_id,
                    report.agent_id,
                    report.job_id,
                    report.state,
                    report.created_at,
                    report.updated_at,
                    Jsonb(report.detail),
                    Jsonb([event.model_dump() for event in report.events]),
                ),
            )

    def runs(self, limit: int) -> list[StoredRun]:
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT {RUN_COLUMNS} FROM agent_runs "
                "ORDER BY created_at DESC, run_id DESC LIMIT %s",
                (limit,),
            ).fetchall()
        return [StoredRun.model_validate(row) for row in rows]

    def run(self, run_id: str) -> StoredRun | None:
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE run_id=%s", (run_id,)
            ).fetchone()
        return StoredRun.model_validate(row) if row else None

    def pending(self, run_id: str) -> ApprovalTicket | None:
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT {APPROVAL_COLUMNS} FROM agent_approvals "
                "WHERE run_id=%s AND consumed_at IS NULL",
                (run_id,),
            ).fetchone()
        return ApprovalTicket.model_validate(row) if row else None

    def decide(
        self, run_id: str, decision: Decision, requested_by: str, requested_at: datetime
    ) -> ApprovalTicket:
        try:
            with self.connect() as conn:
                row = conn.execute(
                    "INSERT INTO agent_approvals(run_id, decision, requested_by, requested_at) "
                    f"VALUES(%s,%s,%s,%s) RETURNING {APPROVAL_COLUMNS}",
                    (run_id, decision, requested_by, requested_at),
                ).fetchone()
        except UniqueViolation:
            raise RequestConflict(DECISION_PENDING) from None
        return ApprovalTicket.model_validate(row)

    def approvals(self, agent_id: str) -> list[ApprovalTicket]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT a.id, a.run_id, a.decision, a.requested_by, a.requested_at "
                "FROM agent_approvals a JOIN agent_runs r ON r.run_id = a.run_id "
                "WHERE r.agent_id=%s AND a.consumed_at IS NULL "
                "ORDER BY a.requested_at, a.id",
                (agent_id,),
            ).fetchall()
        return [ApprovalTicket.model_validate(row) for row in rows]

    def ack(self, approval_id: int, result_state: str, consumed_at: datetime) -> bool:
        with self.connect() as conn:
            conn.execute(
                "UPDATE agent_approvals SET consumed_at=%s, result_state=%s "
                "WHERE id=%s AND consumed_at IS NULL",
                (consumed_at, result_state, approval_id),
            )
            row = conn.execute(
                "SELECT 1 FROM agent_approvals WHERE id=%s", (approval_id,)
            ).fetchone()
        return row is not None
