from datetime import UTC, datetime

from pydantic import ValidationError

from controlplane.core.errors import InvalidRequest, OperationNotFound
from controlplane.domain.fleet import FleetConfig
from controlplane.integrations.codec import (
    cutover_from_status,
    gate_from_decision,
    sample_from_status,
    sample_to_json,
    verify_from_report,
)
from controlplane.repositories.fleet_journal import FleetJournal
from controlplane.schemas.migrations import MigrationIngest, WriterReport

VERIFY_MODES = ("pre", "post")


def _grown(current: int, previous: int | None) -> int:
    return current - previous if previous is not None and current >= previous else 0


def writer_status(report: WriterReport, previous: dict | None) -> dict:
    previous = previous or {}
    counters = previous.get("counters") or {}
    by_target = dict(previous.get("committed_by_target") or {})
    if report.target:
        by_target[report.target] = by_target.get(report.target, 0) + _grown(
            report.committed, counters.get("bot_committed")
        )
    failed = counters.get("queue_rejected", 0) + _grown(report.failed, counters.get("bot_failed"))
    p95 = report.latency_p95_ms / 1000 if report.latency_p95_ms is not None else None
    return {
        "state": "PAUSED" if report.mode == "paused" else "RUNNING",
        "target": report.target,
        "phase": report.mode,
        "queue_length": report.pending,
        "committed_by_target": by_target,
        "counters": {
            "bot_committed": report.committed,
            "bot_failed": report.failed,
            "queue_rejected": failed,
        },
        "latency_seconds": {"p95": p95},
        "rate_per_second": 0,
    }


class IngestService:
    def __init__(self, config: FleetConfig, journal: FleetJournal) -> None:
        self._config = config
        self._journal = journal

    def _ingest_cutover(self, job_id: str, payload: dict) -> None:
        if payload.get("job_id") not in (None, job_id):
            raise InvalidRequest("cutover 상태의 job_id가 경로와 다릅니다.")
        parsed = cutover_from_status(payload)
        if parsed:
            stage, checkpoint, updated_at = parsed
            self._journal.put_cutover(job_id, stage, checkpoint, updated_at)

    def _ingest_verify(self, job_id: str, payload: dict) -> None:
        mode = payload.get("mode")
        if mode not in VERIFY_MODES:
            raise InvalidRequest("verify mode는 pre 또는 post여야 합니다.")
        self._journal.put_verify(job_id, mode, verify_from_report(payload.get("report") or {}))

    def _ingest_writer(self, job_id: str, report: WriterReport) -> None:
        row = self._journal.get_worker_status(job_id)
        at = datetime.now(UTC)
        sample = sample_to_json(
            sample_from_status(writer_status(report, row["status"] if row else None), at)
        )
        self._journal.add_sample(job_id, at, sample)
        self._journal.put_worker_status(job_id, at, sample)

    def ingest(self, job_id: str, body: MigrationIngest) -> None:
        if self._config.migration(job_id) is None:
            raise OperationNotFound("등록된 마이그레이션 작업이 없습니다.")
        try:
            if body.cutover:
                self._ingest_cutover(job_id, body.cutover)
            for record in body.policy_decisions:
                if record.get("job_id") not in (None, job_id):
                    raise InvalidRequest("정책 판정의 job_id가 경로와 다릅니다.")
                self._journal.put_decision(job_id, gate_from_decision(record))
            if body.verify:
                self._ingest_verify(job_id, body.verify)
            if body.writer:
                self._ingest_writer(job_id, body.writer)
        except InvalidRequest:
            raise
        except (KeyError, ValueError, ValidationError):
            raise InvalidRequest("마이그레이션 보고 형식을 확인하세요.") from None
