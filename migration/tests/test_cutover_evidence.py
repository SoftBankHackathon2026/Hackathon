from dataclasses import replace
from pathlib import Path
import json

import pytest

from migration import policy_client as pc
from migration.cutover_evidence import collect, write_atomic, app_info, EvidenceError
from migration.cutover_policy import CutoverPolicyGate, CutoverPolicyError


def setup(config, **overrides):
    target = replace(config.targets[config.job.target], kind='rds', host='demo.rds.amazonaws.com')
    config = replace(config, targets={**config.targets, config.job.target: target})
    instance = {'DBInstanceIdentifier': 'demo', 'Endpoint': {'Address': target.host, 'Port': target.port},
                'DBInstanceArn': 'arn:aws:rds:ap-northeast-2:123456789012:db:demo',
                'DBInstanceStatus': 'available', 'PubliclyAccessible': False}
    args = dict(identifier='demo', region='ap-northeast-2', approved_locations=['ap-northeast-2'],
                app_url='http://127.0.0.1:18080', app_environment='aws', evidence_ttl_seconds=60,
                write_wait_max_seconds=300,
                rds_reader=lambda: {'DBInstances': [instance]},
                app_reader=lambda: {'environment': 'aws', 'database': {'name': target.dbname, 'row_count': '10'}})
    args.update(overrides)
    return config, args, instance


def test_real_opa_accepts_observed_facts(config, state_dir, tmp_path):
    config, args, _ = setup(config)
    doc = collect(config, **args)
    path = tmp_path / 'evidence.json'
    write_atomic(path, doc)
    gate = CutoverPolicyGate(config, state_dir, evidence_file=str(path),
        evaluator=pc.OpaCliEvaluator(Path(__file__).resolve().parents[2] / 'policies'))
    assert gate('pre_cutover')['plan_digest'] == doc['plan']['digest']


@pytest.mark.parametrize('change', ['host', 'port', 'region', 'identifier', 'missing', 'unavailable'])
def test_wrong_rds_never_produces_pass(config, change):
    config, args, instance = setup(config)
    if change == 'host': instance['Endpoint']['Address'] = 'other.rds.amazonaws.com'
    if change == 'port': instance['Endpoint']['Port'] += 1
    if change == 'region': instance['DBInstanceArn'] = instance['DBInstanceArn'].replace('ap-northeast-2', 'us-east-1')
    if change == 'identifier': instance['DBInstanceIdentifier'] = 'other'
    if change == 'missing': instance.pop('PubliclyAccessible')
    if change == 'unavailable': instance['DBInstanceStatus'] = 'starting'
    doc = collect(config, **args)
    assert doc['checks']['target_db_exposure']['status'] == 'error'


def test_public_db_is_denied_by_real_opa(config, state_dir, tmp_path):
    config, args, instance = setup(config)
    instance['PubliclyAccessible'] = True
    path = tmp_path / 'evidence.json'
    write_atomic(path, collect(config, **args))
    gate = CutoverPolicyGate(config, state_dir, evidence_file=str(path),
        evaluator=pc.OpaCliEvaluator(Path(__file__).resolve().parents[2] / 'policies'))
    with pytest.raises(CutoverPolicyError, match='BLOCKED'):
        gate('pre_cutover')


@pytest.mark.parametrize('db', [None, {'name': 'wrong', 'row_count': '1'}, {'name': 'test', 'row_count': True}])
def test_invalid_app_never_passes(config, db):
    config, args, _ = setup(config, app_reader=lambda: {'environment':'aws', 'database':db})
    assert collect(config, **args)['checks']['target_app_db']['status'] != 'pass'


def test_failure_replaces_previous_pass_and_redacts_errors(config, tmp_path):
    config, args, _ = setup(config)
    path = tmp_path / 'evidence.json'
    write_atomic(path, collect(config, **args))
    def failure(): raise RuntimeError('credential=never-persist-this')
    args.update(rds_reader=failure, app_reader=failure)
    write_atomic(path, collect(config, **args))
    assert 'never-persist' not in path.read_text()
    assert all(c['status'] == 'error' for c in json.loads(path.read_text())['checks'].values())
    assert list(tmp_path.glob('.policy-evidence-*')) == []


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'http://user:secret@host', 'https://host?token=x'])
def test_app_rejects_credential_urls_before_network(url):
    with pytest.raises(EvidenceError, match='URL_INVALID'):
        app_info(url)


def test_setup_failure_invalidates_previous_snapshot(monkeypatch, tmp_path):
    from migration.cutover_evidence import main
    output = tmp_path / 'evidence.json'
    output.write_text('{"old":"snapshot"}')
    monkeypatch.setattr('sys.argv', ['collector', '--config', str(tmp_path/'missing.yaml'),
        '--output', str(output), '--rds-identifier', 'demo', '--region', 'ap-northeast-2',
        '--app-url', 'http://localhost', '--app-environment', 'local',
        '--approved-location', 'ap-northeast-2', '--evidence-ttl-seconds', '60',
        '--write-wait-max-seconds', '300'])
    assert main() == 2
    assert json.loads(output.read_text()) == {'error': 'EVIDENCE_COLLECTION_FAILED'}
