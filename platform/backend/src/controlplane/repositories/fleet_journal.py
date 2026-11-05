from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from controlplane.repositories.releases import ReleaseStore
from controlplane.schemas.migrations import ConnectorView, GateDecision, VerifyReport

Row = dict[str, Any]
SAMPLE_RETENTION = "2 days"


class FleetJournal:
    def __init__(self, releases: ReleaseStore) -> None:
        self._releases = releases

    def connect(self):
        return self._releases.connect()

    def add_sample(self, job_id: str, at: datetime, sample: Row) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO fleet_worker_samples(job_id, at, sample) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (job_id, at, Jsonb(sample)),
            )
            conn.execute(
                f"DELETE FROM fleet_worker_samples WHERE job_id=%s AND at < now() - interval '{SAMPLE_RETENTION}'",
                (job_id,),
            )

    def list_samples(self, job_id: str, since: datetime, until: datetime) -> list[Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT at, sample FROM fleet_worker_samples WHERE job_id=%s AND at BETWEEN %s AND %s ORDER BY at",
                (job_id, since, until),
            ).fetchall()

    def put_worker_status(self, job_id: str, at: datetime, status: Row) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO fleet_worker_status(job_id, status, observed_at) VALUES(%s,%s,%s) "
                "ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, observed_at=excluded.observed_at",
                (job_id, Jsonb(status), at),
            )

    def get_worker_status(self, job_id: str) -> Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT status, observed_at FROM fleet_worker_status WHERE job_id=%s", (job_id,)
            ).fetchone()

    def put_cutover(self, job_id: str, stage: str, checkpoint: Row, updated_at: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO fleet_cutover(job_id, stage, checkpoint, updated_at) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(job_id) DO UPDATE SET stage=excluded.stage, checkpoint=excluded.checkpoint, "
                "updated_at=excluded.updated_at WHERE fleet_cutover.updated_at <= excluded.updated_at",
                (job_id, stage, Jsonb(checkpoint), updated_at),
            )
            conn.execute(
                "INSERT INTO fleet_cutover_stages(job_id, stage, first_seen_at) VALUES(%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (job_id, stage, updated_at),
            )

    def get_cutover(self, job_id: str) -> tuple[Row | None, list[Row]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT stage, checkpoint, updated_at FROM fleet_cutover WHERE job_id=%s", (job_id,)
            ).fetchone()
            stages = conn.execute(
                "SELECT stage, first_seen_at FROM fleet_cutover_stages WHERE job_id=%s ORDER BY first_seen_at",
                (job_id,),
            ).fetchall()
        return row, stages

    def put_decision(self, job_id: str, decision: GateDecision) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO fleet_gate_decisions(decision_id, job_id, stage, outcome, reason_codes, "
                "policy_version, evaluated_at) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    decision.decision_id,
                    job_id,
                    decision.stage,
                    decision.outcome,
                    Jsonb(decision.reason_codes),
                    decision.policy_version,
                    decision.evaluated_at,
                ),
            )

    def list_decisions(self, job_id: str) -> list[GateDecision]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT decision_id, stage, outcome, reason_codes, policy_version, evaluated_at "
                "FROM fleet_gate_decisions WHERE job_id=%s ORDER BY evaluated_at",
                (job_id,),
            ).fetchall()
        return [GateDecision.model_validate(row) for row in rows]

    def put_verify(self, job_id: str, mode: str, report: VerifyReport) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO fleet_verify_reports(job_id, mode, report) VALUES(%s,%s,%s) "
                "ON CONFLICT(job_id, mode) DO UPDATE SET report=excluded.report, received_at=now()",
                (job_id, mode, Jsonb(report.model_dump())),
            )

    def list_verify(self, job_id: str) -> dict[str, VerifyReport]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT mode, report FROM fleet_verify_reports WHERE job_id=%s", (job_id,)
            ).fetchall()
        return {row["mode"]: VerifyReport.model_validate(row["report"]) for row in rows}

    def put_connectors(self, job_id: str, at: datetime, connectors: list[ConnectorView]) -> None:
        payload = [connector.model_dump() for connector in connectors]
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO fleet_connectors(job_id, connectors, observed_at) VALUES(%s,%s,%s) "
                "ON CONFLICT(job_id) DO UPDATE SET connectors=excluded.connectors, observed_at=excluded.observed_at",
                (job_id, Jsonb(payload), at),
            )

    def list_connectors(self, job_id: str) -> list[ConnectorView]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT connectors FROM fleet_connectors WHERE job_id=%s", (job_id,)
            ).fetchone()
        return [ConnectorView.model_validate(item) for item in row["connectors"]] if row else []

    def put_facts(self, env_id: str, at: datetime, facts: Row) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO fleet_environment_facts(env_id, facts, observed_at) VALUES(%s,%s,%s) "
                "ON CONFLICT(env_id) DO UPDATE SET facts=excluded.facts, observed_at=excluded.observed_at",
                (env_id, Jsonb(facts), at),
            )

    def get_facts(self, env_id: str) -> Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT facts, observed_at FROM fleet_environment_facts WHERE env_id=%s", (env_id,)
            ).fetchone()
