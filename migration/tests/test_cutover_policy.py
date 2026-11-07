from pathlib import Path
import json
import pytest
from migration import policy_client as pc
from migration.cutover_policy import CutoverPolicyError, CutoverPolicyGate, execution_digest
from tests.test_cutover import _build, fakes as fakes

@pytest.fixture
def setup_gate(config, tmp_path, state_dir):
    if not pc.OpaCliEvaluator.available(): pytest.skip('OPA required')
    plan = {'source': config.job.source, 'target': config.job.target,
            'execution_digest': execution_digest(config), 'approved_locations': ['test'],
            'limits': {'evidence_ttl_seconds': 60, 'write_wait_max_seconds': 600}}
    plan['digest'] = pc.digest_of(plan)
    doc = {'job_id': config.job.job_id, 'plan': plan, 'checks': {
        'target_db_exposure': pc.build_check('pass', {'public': False}, source='test'),
        'target_location': pc.build_check('pass', {'location': 'test'}, source='test'),
        'target_app_db': pc.build_check('pass', {'db_connected': True, 'order_query_ok': True}, source='test')}}
    file = tmp_path / 'evidence.json'
    file.write_text(json.dumps(doc))
    return CutoverPolicyGate(config, state_dir, evidence_file=str(file),
        evaluator=pc.OpaCliEvaluator(Path(__file__).resolve().parents[2] / 'policies')), doc, file


def test_real_opa_allows_and_records(config, state_dir, setup_gate):
    gate, doc, _ = setup_gate
    assert gate('pre_cutover')['plan_digest'] == doc['plan']['digest']
    store = pc.DecisionStore(state_dir)
    try: assert store.latest(config.job.job_id, 'pre_cutover').allowed
    finally: store.close()


@pytest.mark.parametrize('case', ['deny', 'stale', 'future', 'missing', 'wrong_job', 'wrong_config', 'secret'])
def test_bad_evidence_blocks(case, setup_gate):
    gate, doc, file = setup_gate
    if case == 'deny': doc['checks']['target_db_exposure']['detail']['public'] = True
    if case == 'stale': doc['checks']['target_app_db']['observed_at'] = '2000-01-01T00:00:00Z'
    if case == 'future': doc['checks']['target_app_db']['observed_at'] = '2099-01-01T00:00:00Z'
    if case == 'missing': del doc['checks']['target_app_db']
    if case == 'wrong_job': doc['job_id'] = 'other'
    if case == 'wrong_config': doc['plan']['execution_digest'] = 'wrong'
    if case == 'secret': doc['checks']['target_app_db']['detail']['password'] = 'must-not-record'
    file.write_text(json.dumps(doc))
    with pytest.raises(CutoverPolicyError): gate('pre_cutover')


def test_evidence_is_reloaded(setup_gate):
    gate, doc, file = setup_gate
    gate('pre_cutover')
    doc['checks']['target_db_exposure']['detail']['public'] = True
    file.write_text(json.dumps(doc))
    with pytest.raises(CutoverPolicyError): gate('pre_cutover')


def test_missing_configuration_blocks(config, state_dir, monkeypatch):
    monkeypatch.delenv('CUTOVER_POLICY_EVIDENCE', raising=False)
    monkeypatch.delenv('POLICY_OPA_URL', raising=False)
    with pytest.raises(CutoverPolicyError, match='NOT_CONFIGURED'):
        CutoverPolicyGate(config, state_dir)('pre_start')


def test_unreachable_opa_blocks(config, state_dir, setup_gate):
    _, _, file = setup_gate
    gate = CutoverPolicyGate(config, state_dir, evidence_file=str(file),
                            evaluator=pc.OpaHttpEvaluator('http://127.0.0.1:1', timeout=.1))
    with pytest.raises(CutoverPolicyError, match='BLOCKED'): gate('pre_cutover')


@pytest.mark.parametrize('stage', ['stage_copying', 'stage_fencing', 'stage_switching'])
def test_direct_calls_cannot_bypass(stage, config, state_dir, fakes):
    cut = _build(config, state_dir, fakes)
    def deny(*args): raise CutoverPolicyError('denied')
    cut.policy_gate = deny
    with pytest.raises(CutoverPolicyError): getattr(cut, stage)()
    assert fakes['db'].calls == []
    assert fakes['worker'].calls == []
    assert fakes['connect'].puts == []


def test_changed_plan_after_restart_blocks(config, state_dir, fakes):
    cut = _build(config, state_dir, fakes)
    cut._policy_guard('pre_start')
    cut.store.close()
    cut = _build(config, state_dir, fakes)
    cut.policy_gate = lambda *args: {'plan_digest': 'changed', 'execution_digest': 'test'}
    with pytest.raises(Exception, match='저장된 작업'): cut.stage_fencing()
    assert not fakes['worker'].calls


def test_wait_limit_blocks_before_grant(config, state_dir, fakes, setup_gate):
    gate, _, _ = setup_gate
    cut = _build(config, state_dir, fakes)
    cut.policy_gate = gate
    cut._save(fence_started_at=1)
    fakes['db'].app_insert['onprem'] = False
    with pytest.raises(CutoverPolicyError): cut.stage_switching()
    assert 'grant:aws' not in fakes['db'].calls


def test_mismatched_allow_response_is_recorded_as_error(config, state_dir, setup_gate):
    gate, _, _ = setup_gate
    original = gate.evaluator
    class WrongResponse:
        name = 'wrong-response'
        def evaluate(self, value):
            result, opa_id = original.evaluate(value)
            result['plan_digest'] = 'another-plan'
            return result, opa_id
    gate.evaluator = WrongResponse()
    with pytest.raises(CutoverPolicyError): gate('pre_cutover')
    store = pc.DecisionStore(state_dir)
    try: assert store.latest(config.job.job_id, 'pre_cutover').outcome == pc.ERROR
    finally: store.close()


def test_local_final_facts_override_external_claims(config, state_dir, fakes, setup_gate):
    import time
    gate, _, _ = setup_gate
    cut = _build(config, state_dir, fakes)
    cut.policy_gate = gate
    cut._save(fence_started_at=time.time())
    # Original source can still write: no target grant regardless of external claims.
    with pytest.raises(Exception, match='원본 쓰기 차단'): cut.stage_switching()
    assert not fakes['db'].calls


def test_transient_observation_reloads_and_recovers(setup_gate, monkeypatch):
    gate, doc, file = setup_gate
    good = doc["checks"]["target_app_db"]
    doc["checks"]["target_app_db"] = pc.build_check("error", {"code": "APP_TIMEOUT"}, source="test")
    file.write_text(json.dumps(doc))
    sleeps = []
    def recover(seconds):
        sleeps.append(seconds)
        doc["checks"]["target_app_db"] = good
        file.write_text(json.dumps(doc))
    monkeypatch.setattr(gate, "_sleep", recover, raising=False)
    assert gate("pre_cutover")["stage"] == "pre_cutover"
    assert sleeps == [2]


@pytest.mark.parametrize("code,expected_sleeps", [("APP_TIMEOUT", 2), ("APP_DB_UNAVAILABLE", 0),
                                                  ("APP_TLS_FAILED", 0), ("APP_EVIDENCE_UNAVAILABLE", 0)])
def test_only_known_transient_errors_retry_bounded(setup_gate, monkeypatch, code, expected_sleeps):
    gate, doc, file = setup_gate
    doc["checks"]["target_app_db"] = pc.build_check("error", {"code": code}, source="test")
    file.write_text(json.dumps(doc))
    sleeps = []
    monkeypatch.setattr(gate, "_sleep", sleeps.append)
    with pytest.raises(CutoverPolicyError):
        gate("pre_cutover")
    assert len(sleeps) == expected_sleeps


def test_retry_elapsed_wait_is_not_reset(setup_gate, monkeypatch):
    gate, _, _ = setup_gate
    clock = [10.0]
    monkeypatch.setattr(gate, "_clock", lambda: clock[0])
    monkeypatch.setattr(gate, "_sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    elapsed = []
    def evaluate(stage, checks):
        elapsed.append(checks["write_wait"]["detail"]["elapsed_seconds"])
        raise CutoverPolicyError("transient", retryable=True)
    monkeypatch.setattr(gate, "_evaluate_once", evaluate)
    checks = {"write_wait": pc.build_check("pass", {"elapsed_seconds": 100}, source="test")}
    with pytest.raises(CutoverPolicyError):
        gate("final_verify", checks)
    assert elapsed == [100, 102, 104]
    assert checks["write_wait"]["detail"]["elapsed_seconds"] == 100
