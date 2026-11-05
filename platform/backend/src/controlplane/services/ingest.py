from pydantic import ValidationError

from controlplane.core.errors import InvalidRequest, OperationNotFound
from controlplane.domain.fleet import FleetConfig
from controlplane.integrations.codec import (
    cutover_from_status,
    gate_from_decision,
    verify_from_report,
)
from controlplane.repositories.fleet_journal import FleetJournal
from controlplane.schemas.migrations import MigrationIngest

VERIFY_MODES = ("pre", "post")


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
        except InvalidRequest:
            raise
        except (KeyError, ValueError, ValidationError):
            raise InvalidRequest("마이그레이션 보고 형식을 확인하세요.") from None
