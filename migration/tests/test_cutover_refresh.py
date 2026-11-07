import json
from pathlib import Path

import pytest

from migration import policy_client as pc
from migration.cutover_policy import CutoverPolicyGate, CutoverPolicyError
from migration.cutover_refresh import read_settings, describe_rds_sdk
from tests.test_cutover_evidence import setup
from tests.test_cutover import _build, fakes as fakes


@pytest.fixture
def runtime(config, state_dir, tmp_path, monkeypatch):
    if not pc.OpaCliEvaluator.available():
        pytest.skip('OPA required; policies CI installs OPA')
    config, args, instance = setup(config)
    reader = args.pop('app_reader')
    args.pop('rds_reader')
    settings = tmp_path/'collector.json'
    settings.write_text(json.dumps(args))
    output = tmp_path/'evidence.json'
    calls = []
    def observed(*a):
        calls.append(a)
        return {'DBInstances': [instance]}
    monkeypatch.setattr('migration.cutover_refresh.describe_rds_sdk', observed)
    monkeypatch.setattr('migration.cutover_refresh.app_info', lambda url: reader())
    gate = CutoverPolicyGate(config, state_dir, evidence_file=str(output),
        collector_settings=str(settings),
        evaluator=pc.OpaCliEvaluator(Path(__file__).resolve().parents[2]/'policies'))
    return config, gate, settings, output, instance, calls


def test_each_boundary_refreshes_and_public_change_denies(runtime):
    _, gate, _, output, instance, calls = runtime
    first = gate('pre_start')
    gate('pre_cutover')
    assert len(calls) == 2
    instance['PubliclyAccessible'] = True
    with pytest.raises(CutoverPolicyError, match=r':deny$'):
        gate('pre_cutover')
    assert len(calls) == 3
    assert json.loads(output.read_text())['checks']['target_db_exposure']['detail']['public'] is True
    assert first['decision_id']


def test_sdk_failure_replaces_old_allow_with_error(runtime, monkeypatch):
    _, gate, _, output, _, _ = runtime
    gate('pre_start')
    def broken(*a): raise RuntimeError('password=do-not-persist')
    monkeypatch.setattr('migration.cutover_refresh.describe_rds_sdk', broken)
    with pytest.raises(CutoverPolicyError, match='BLOCKED'):
        gate('pre_cutover')
    assert json.loads(output.read_text())['checks']['target_db_exposure']['status'] == 'error'
    assert 'do-not-persist' not in output.read_text()


def test_missing_settings_invalidates_old_allow(runtime):
    _, gate, settings, output, _, _ = runtime
    gate('pre_start')
    settings.unlink()
    with pytest.raises(CutoverPolicyError, match='REFRESH_FAILED'):
        gate('pre_cutover')
    assert json.loads(output.read_text()) == {'error': 'EVIDENCE_REFRESH_FAILED'}


def test_failed_write_never_reuses_previous_allow(runtime, monkeypatch):
    _, gate, _, output, _, _ = runtime
    gate('pre_start')
    previous = output.read_bytes()
    def no_disk(*a): raise OSError('disk unavailable')
    monkeypatch.setattr('migration.cutover_refresh.write_atomic', no_disk)
    with pytest.raises(CutoverPolicyError, match='REFRESH_FAILED'):
        gate('pre_cutover')
    assert output.read_bytes() == previous


def test_changed_limits_do_not_silently_replace_plan(runtime):
    _, gate, settings, _, _, _ = runtime
    gate('pre_start')
    value = json.loads(settings.read_text())
    value['write_wait_max_seconds'] += 1
    settings.write_text(json.dumps(value))
    with pytest.raises(CutoverPolicyError, match='PLAN_CHANGED'):
        gate('pre_cutover')


def test_refresh_deny_blocks_cutover_before_db_and_worker(runtime, state_dir, fakes):
    config, gate, _, _, instance, _ = runtime
    cut = _build(config, state_dir, fakes)
    cut.policy_gate = gate
    instance['PubliclyAccessible'] = True
    try:
        with pytest.raises(CutoverPolicyError, match=r':deny$'):
            cut.stage_fencing()
        assert not fakes['db'].calls
        assert not fakes['worker'].calls
    finally:
        cut.store.close()


def test_settings_reject_credentials_and_size(tmp_path):
    path = tmp_path/'settings.json'
    path.write_text(json.dumps({'aws_secret_access_key': 'never-use'}))
    with pytest.raises(ValueError): read_settings(path)
    path.write_text(' ' * 65537)
    with pytest.raises(ValueError): read_settings(path)


def test_sdk_uses_exact_instance_and_default_credentials(monkeypatch):
    boto3 = pytest.importorskip("boto3", reason="policy-runtime extra required; policies CI installs it")
    seen = {}
    class Client:
        def close(self): seen["closed"] = True
        def describe_db_instances(self, **kwargs):
            seen['request'] = kwargs
            return {'DBInstances': []}
    class Session:
        def __init__(self, **kwargs): seen['session'] = kwargs
        def client(self, name, **kwargs):
            seen['service'], seen['client'] = name, kwargs
            return Client()
    monkeypatch.setattr(boto3, 'Session', Session)
    assert describe_rds_sdk('exact-db', 'ap-northeast-2', None) == {'DBInstances': []}
    assert seen['request'] == {'DBInstanceIdentifier': 'exact-db'}
    assert seen['session'] == {'profile_name': None}
    assert seen['client']['config'].ignore_configured_endpoint_urls


def test_real_sdk_client_protocol_without_network(monkeypatch):
    boto3 = pytest.importorskip("boto3", reason="policy-runtime extra required; policies CI installs it")
    from botocore.stub import Stubber
    session = boto3.session.Session(aws_access_key_id='test-only', aws_secret_access_key='test-only')
    client = session.client('rds', region_name='ap-northeast-2')
    with Stubber(client) as stub:
        stub.add_response('describe_db_instances', {'DBInstances': []}, {'DBInstanceIdentifier': 'demo'})
        monkeypatch.setattr(session, 'client', lambda *a, **kw: client)
        monkeypatch.setattr(boto3, 'Session', lambda **kw: session)
        assert describe_rds_sdk('demo', 'ap-northeast-2', None) == {'DBInstances': []}
        stub.assert_no_pending_responses()
