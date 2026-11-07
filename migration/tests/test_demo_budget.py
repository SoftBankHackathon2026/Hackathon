"""Real worker/SQLite and cutover orchestration; CDC/backend/policy are simulated."""
from dataclasses import replace
import time

import pytest

from migration.config import ConfigError, parse_config
from migration.cutover import ApprovalRequired, COMPLETED, Cutover, CutoverError, Ports, VERIFYING
from migration.loadgen import Worker, WorkerSettings
from migration.state import CutoverStore, RequestStore
from tests.test_cutover import FakeConnect, FakeDb, FakeVerify, FakeWorker
from tests.test_loadgen import MemoryBackend


def test_800_plus_100_plus_100_and_resume_after_target_writes(config, state_dir, monkeypatch):
    config = replace(config, cutover=replace(config.cutover, demo_total_rows=1000,
                     min_live_rows_after_snapshot=100, min_rows_after_switch=100,
                     poll_interval_seconds=0.01))
    backends = {name: MemoryBackend(name) for name in ('onprem', 'aws')}
    requests = RequestStore(state_dir)
    worker = Worker(WorkerSettings(run_id='demo', phase='onprem_live', target='onprem',
                    max_generated=200, rate_per_second=500, state_dir=state_dir, known_targets=('onprem', 'aws')),
                    requests, backends.__getitem__)
    connect = FakeConnect()

    class DemoDb(FakeDb):
        def count_rows(self, target, phase=None):
            if connect.connectors:
                self.seed['aws'] = 800
                backends['aws'].rows.update(backends['onprem'].rows)
            return self.seed[target] + (0 if phase == 'seed' else len(backends[target].rows))

        def precheck(self, target, *, is_source):
            return {**super().precheck(target, is_source=is_source), 'row_count': self.count_rows(target)}

        def latest_ids(self, target, n):
            return list(backends[target].rows)[-n:]

        def row_exists(self, target, request_id):
            return request_id in backends[target].rows

    db = DemoDb(); db.seed = {'onprem': 800, 'aws': 0}
    ports = Ports(worker=worker, connect=connect, db=db, verify=FakeVerify(), request_store=requests)
    docs = {kind: {'name': getattr(config.job, kind + '_connector'), 'config': {}}
            for kind in ('source', 'sink')}

    def cutover():
        return Cutover(config, CutoverStore(state_dir), ports, connector_docs=docs, approve='local-test',
                       policy_gate=lambda *a: {'plan_digest': 'test', 'execution_digest': 'test'})

    worker.start()
    try:
        first = cutover(); first.approve = None
        with pytest.raises(ApprovalRequired):
            first.run()
        assert db.count_rows('onprem') == db.count_rows('aws') == 900
        assert worker.status()['generated_total'] == 100
        # Model a process crash after target resume but before saving VERIFYING.
        second = cutover()
        original_save = second._save
        def crash_after_resume(stage=None, **checkpoint):
            if stage == VERIFYING:
                raise RuntimeError('simulated crash after target resume')
            return original_save(stage, **checkpoint)
        monkeypatch.setattr(second, '_save', crash_after_resume)
        with pytest.raises(RuntimeError, match='simulated crash'):
            second.run()
        deadline = time.monotonic() + 5
        while worker.status()['committed_by_target'].get('aws', 0) != 100:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        resumed = cutover()
        assert resumed.run(force_retry=True) == COMPLETED
        assert resumed.job.checkpoint['committed_on_target_before'] == 0
        assert worker.status()['generated_total'] == 200
        assert db.count_rows('onprem') == 900
        assert db.count_rows('aws') == 1000
        assert worker.generate_one() is None
        assert len(backends['onprem'].rows) == 100
        assert sum(row[1] == config.job.live_phase_after for row in backends['aws'].rows.values()) == 100
    finally:
        worker.stop(); requests.close()


def test_demo_configuration_rejects_a_budget_that_cannot_finish(config):
    with pytest.raises(ConfigError):
        replace(config.cutover, demo_total_rows=1000)  # existing 3/2 thresholds
    with pytest.raises(ConfigError):
        replace(config.cutover, demo_total_rows=1001, min_live_rows_after_snapshot=100, min_rows_after_switch=100)


@pytest.mark.parametrize('value', [None, 1000])
def test_yaml_demo_option_keeps_numeric_or_null(config, value):
    from tests.conftest import RAW_CONFIG
    raw = {**RAW_CONFIG, 'cutover': {'demo_total_rows': value}}
    assert parse_config(raw).cutover.demo_total_rows == value


@pytest.mark.parametrize('legacy_worker', [True, False])
def test_demo_precheck_refuses_old_worker_or_existing_data(config, state_dir, legacy_worker):
    config = replace(config, cutover=replace(config.cutover, demo_total_rows=1000,
                     min_live_rows_after_snapshot=100, min_rows_after_switch=100))
    db = FakeDb()
    if legacy_worker:
        db.rows['onprem'] = db.seed['onprem'] = 800
    worker = FakeWorker('onprem')
    status = worker.status
    if not legacy_worker:
        worker.status = lambda: {**status(), 'max_generated': 200, 'generation_limit': 0,
                                 'generated_total': 0, 'queue_length': 0}
    cut = Cutover(config, CutoverStore(state_dir), Ports(worker=worker, db=db,
                  connect=FakeConnect(), verify=FakeVerify()), connector_docs={})
    with pytest.raises(CutoverError, match='1000-row demo'):
        cut.stage_precheck()
    assert not db.calls, 'precheck must refuse before changing DB permissions or CDC'
