from controlplane.domain.fleet import FleetConfig
from controlplane.integrations.codec import sample_from_json
from controlplane.integrations.defaults import DEFAULT_FLEET
from controlplane.schemas.migrations import MigrationIngest, WriterReport
from controlplane.services.ingest import IngestService, writer_status


def report(**overrides):
    base = {
        "target": "onprem_return",
        "mode": "running",
        "committed": 100,
        "failed": 5,
        "pending": 2,
    }
    return WriterReport(**{**base, **overrides})


def test_the_first_report_sets_a_baseline_without_counting_old_writes():
    status = writer_status(report(latency_p95_ms=250), None)
    assert status["committed_by_target"] == {"onprem_return": 0}
    assert status["counters"] == {"bot_committed": 100, "bot_failed": 5, "queue_rejected": 0}
    assert (status["state"], status["phase"], status["queue_length"]) == ("RUNNING", "running", 2)
    assert status["latency_seconds"] == {"p95": 0.25}


def test_new_commits_go_to_the_current_ingress_target():
    first = writer_status(report(), None)
    second = writer_status(report(committed=130, failed=7), first)
    assert second["committed_by_target"] == {"onprem_return": 30}
    assert second["counters"]["queue_rejected"] == 2
    switched = writer_status(report(target="aws", committed=150, mode="paused"), second)
    assert switched["committed_by_target"] == {"onprem_return": 30, "aws": 20}
    assert switched["state"] == "PAUSED"


def test_a_restarted_bot_or_unknown_target_does_not_add_writes():
    first = writer_status(report(committed=500), None)
    reset = writer_status(report(committed=10, failed=1), first)
    assert reset["committed_by_target"] == {"onprem_return": 0}
    assert reset["counters"]["queue_rejected"] == 0
    unknown = writer_status(report(target="", committed=40), reset)
    assert unknown["committed_by_target"] == {"onprem_return": 0}
    assert unknown["counters"]["bot_committed"] == 40


class Journal:
    def __init__(self):
        self.samples, self.status = [], None

    def get_worker_status(self, job_id):
        return {"status": self.status} if self.status else None

    def add_sample(self, job_id, at, sample):
        self.samples.append((job_id, at, sample))

    def put_worker_status(self, job_id, at, status):
        self.status = status


def config_with(job_id):
    config: FleetConfig = DEFAULT_FLEET.model_copy(deep=True)
    config.migrations[0] = config.migrations[0].model_copy(update={"job_id": job_id})
    return config


def test_ingest_stores_writer_reports_as_worker_samples():
    journal = Journal()
    service = IngestService(config_with("onprem_to_aws_009"), journal)
    service.ingest("onprem_to_aws_009", MigrationIngest(writer=report()))
    service.ingest("onprem_to_aws_009", MigrationIngest(writer=report(committed=160)))
    assert [job for job, _, _ in journal.samples] == ["onprem_to_aws_009"] * 2
    job, at, payload = journal.samples[-1]
    sample = sample_from_json(at, payload)
    assert sample.committed_by_target == {"onprem_return": 60}
    assert (sample.state, sample.target, sample.queue_length) == ("RUNNING", "onprem_return", 2)
    assert journal.status == payload


def test_reports_without_writer_do_not_touch_samples():
    journal = Journal()
    IngestService(config_with("onprem_to_aws_009"), journal).ingest(
        "onprem_to_aws_009", MigrationIngest(policy_decisions=[])
    )
    assert journal.samples == []
