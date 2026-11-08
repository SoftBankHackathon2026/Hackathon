import logging
from datetime import datetime

from pydantic import ValidationError

from controlplane.domain.cloud_security import CloudSecurityFacts
from controlplane.domain.fleet import (
    ClusterFacts,
    CutoverRecord,
    EnvironmentSnapshot,
    FleetConfig,
    MigrationConfig,
    MigrationState,
    WorkerSample,
)
from controlplane.domain.migration import Stage
from controlplane.integrations.codec import facts_from_json, sample_from_json, worker_view
from controlplane.repositories.fleet_journal import FleetJournal

LOG = logging.getLogger(__name__)


class JournalFleet:
    mode = "live"

    def __init__(self, config: FleetConfig, journal: FleetJournal) -> None:
        self._config = config
        self._journal = journal

    def list_environments(self) -> list[EnvironmentSnapshot]:
        snapshots = []
        for env in self._config.environments:
            row = self._journal.get_facts(env.id)
            facts = (
                facts_from_json(row["facts"])
                if row
                else ClusterFacts(observed_at=None, connected=False)
            )
            snapshots.append(
                EnvironmentSnapshot(config=env, cluster=facts, cloud=self._cloud(env.id))
            )
        return snapshots

    def _cloud(self, env_id: str) -> CloudSecurityFacts | None:
        row = self._journal.get_cloud_security(env_id)
        if row is None:
            return None
        try:
            return CloudSecurityFacts.model_validate(row["facts"])
        except ValidationError:
            # 저장 형식이 바뀐 옛 행은 버리고 신고값으로 폴백한다. 다음 수집에서 덮어쓴다.
            LOG.warning("env=%s cloud security row invalid", env_id)
            return None

    def _cutover(self, job_id: str) -> CutoverRecord | None:
        row, stages = self._journal.get_cutover(job_id)
        if row is None:
            return None
        return CutoverRecord(
            stage=Stage(row["stage"]),
            checkpoint=row["checkpoint"],
            updated_at=row["updated_at"],
            stage_times=[(Stage(s["stage"]), s["first_seen_at"]) for s in stages],
        )

    def _state(self, job: MigrationConfig) -> MigrationState:
        verify = self._journal.list_verify(job.job_id)
        status = self._journal.get_worker_status(job.job_id)
        worker = None
        if status:
            worker = worker_view(sample_from_json(status["observed_at"], status["status"]))
        return MigrationState(
            config=job,
            cutover=self._cutover(job.job_id),
            gates=self._journal.list_decisions(job.job_id),
            verify_pre=verify.get("pre"),
            verify_post=verify.get("post"),
            connectors=self._journal.list_connectors(job.job_id),
            worker=worker,
        )

    def list_migrations(self) -> list[MigrationState]:
        return [self._state(job) for job in self._config.migrations]

    def list_samples(self, job_id: str, since: datetime, until: datetime) -> list[WorkerSample]:
        rows = self._journal.list_samples(job_id, since, until)
        return [sample_from_json(row["at"], row["sample"]) for row in rows]
