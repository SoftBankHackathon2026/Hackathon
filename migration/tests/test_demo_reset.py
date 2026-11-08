"""Reset boundaries and real two-database archive/repeat behavior."""
from dataclasses import replace
import importlib.util
import os
from pathlib import Path

import psycopg
import pytest

from migration.config import Config, JobConfig, TargetConfig, UserSpec, FullMigrationConfig
from migration import demo_reset as resetter


def config_for(port=5432):
    return Config(
        targets={n: TargetConfig(n, '127.0.0.1', port, database, sslmode='disable',
            users={'admin': UserSpec('postgres', 'DEMO_TEST_PASSWORD')})
            for n, database in [('onprem_return', 'migration_demo_return'), ('aws', 'migration_demo')]},
        job=JobConfig(source='onprem_return', target='aws'),
        full_migration=FullMigrationConfig(destination_control_url='https://receipt.test/control',
            destination_receipt_url='https://receipt.test', destination_token_env='CONTROL_TOKEN',
            destination_api_token_env='API_TOKEN', destination_target='aws_eks',
            forward_target='aws_receipt', destination_app_version='test'),
    )


def test_reset_disabled_and_strict_scope(monkeypatch):
    config = config_for()
    monkeypatch.delenv('DEMO_RESET_ENABLED', raising=False)
    with pytest.raises(ValueError, match='DISABLED'):
        resetter.validate(config, 'demo_1')
    monkeypatch.setenv('DEMO_RESET_ENABLED', 'true')
    for cycle in ('../x', 'a.b', 'A', '', 'a' * 49):
        with pytest.raises(ValueError, match='CYCLE'):
            resetter.validate(config, cycle)
    with pytest.raises(ValueError, match='SCOPE'):
        resetter.validate(replace(config, job=replace(config.job, table='public.business')), 'demo_1')
    with pytest.raises(ValueError, match='FULL_MIGRATION'):
        resetter.validate(replace(config, full_migration=None), 'demo_1')
    resetter.validate(config, 'demo_1')


@pytest.mark.parametrize('snapshot', [
    {'connectors': ['another-job'], 'databases': {}},
    {'connectors': [], 'active_cutovers': [123], 'databases': {}},
    {'connectors': [], 'databases': {'aws': {'active_slots': ['cloud_to_gcp']}}},
])
def test_active_work_blocks_reset(snapshot):
    with pytest.raises(ValueError):
        resetter.require_idle(snapshot)


def test_cli_confirmation_precedes_stopping_processes(tmp_path):
    spec = importlib.util.spec_from_file_location('demo_cli', Path(__file__).parents[1] / 'scripts/demo-cycle.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    from types import SimpleNamespace
    demo = module.Demo(SimpleNamespace(cycle='demo_1', state_dir=str(tmp_path), confirm_reset=None))
    with pytest.raises(ValueError, match='confirm-reset'):
        demo.reset()
    assert not demo.file.exists()


@pytest.mark.skipif(not os.getenv('DEMO_TEST_PG_PORT'), reason='isolated PostgreSQL not provided')
def test_two_real_databases_archive_reset_twice_and_preserve_other_tables(monkeypatch, tmp_path):
    port = int(os.environ['DEMO_TEST_PG_PORT'])
    config = config_for(port)
    monkeypatch.setenv('DEMO_TEST_PASSWORD', 'test-only')
    monkeypatch.setenv('DEMO_RESET_ENABLED', 'true')
    monkeypatch.setattr(resetter.ConnectClient, 'list_connectors', lambda self: [])
    with psycopg.connect(host='127.0.0.1', port=port, user='postgres', dbname='postgres', autocommit=True) as c:
        for name in ('migration_demo_return', 'migration_demo'):
            c.execute('DROP DATABASE IF EXISTS ' + name + ' WITH (FORCE)')
            c.execute('CREATE DATABASE ' + name)
    ddl = (Path(__file__).parents[1] / 'sql/001_demo.sql').read_text()
    for target in config.targets.values():
        with psycopg.connect(host='127.0.0.1', port=port, user='postgres', dbname=target.dbname) as c:
            c.execute(ddl)
            c.execute('CREATE TABLE keep_me (value int)'); c.execute('INSERT INTO keep_me VALUES (42)')
            c.execute("INSERT INTO migration_demo VALUES ('00000000-0000-0000-0000-000000000001','old','old',1,'old',now())")
    assert resetter.reset(config, 'demo_1', tmp_path)['source_rows'] == 800
    initial = resetter.inspect(config)['databases']
    assert initial['onprem_return']['rows'] == 800 and not initial['onprem_return']['app_insert']
    assert initial['aws']['rows'] == 0 and not initial['aws']['app_insert']
    resetter.open_source(config, 'demo_1', tmp_path)
    assert resetter.inspect(config)['databases']['onprem_return']['app_insert']
    for target in config.targets.values():
        with psycopg.connect(host='127.0.0.1', port=port, user='postgres', dbname=target.dbname) as c:
            assert c.execute('SELECT count(*) FROM demo_archive.demo_1').fetchone()[0] == 1
            assert c.execute('SELECT value FROM keep_me').fetchone()[0] == 42
    with psycopg.connect(host='127.0.0.1', port=port, user='postgres', dbname='migration_demo') as c:
        c.execute("INSERT INTO migration_demo VALUES ('00000000-0000-0000-0000-000000000002','new','new',1,'new',now())")
    assert resetter.reset(config, 'demo_1', tmp_path)['already_reset']
    assert resetter.inspect(config)['databases']['aws']['rows'] == 1
    resetter.reset(config, 'demo_2', tmp_path)
    assert resetter.inspect(config)['databases']['aws']['rows'] == 0
    with psycopg.connect(host='127.0.0.1', port=port, user='postgres', dbname='migration_demo') as c:
        assert c.execute('SELECT count(*) FROM demo_archive.demo_2').fetchone()[0] == 1


def test_reset_gate_excludes_reporter_and_other_cycles(tmp_path):
    root = tmp_path / 'demo-cycles'
    with resetter.command_lease(tmp_path) as allowed:
        assert allowed
        with pytest.raises(BlockingIOError):
            resetter.gate(root, 'demo_1')
    resetter.gate(root, 'demo_1')
    with resetter.command_lease(tmp_path) as allowed:
        assert not allowed
    with pytest.raises(ValueError, match='OTHER_RESET'):
        resetter.gate(root, 'demo_2')
    resetter.gate(root, 'demo_1', finish=True)
    with resetter.command_lease(tmp_path) as allowed:
        assert allowed


def test_reporter_does_not_claim_commands_during_reset(monkeypatch, tmp_path):
    path = Path(__file__).parents[2] / 'platform/deploy/onprem/fleet_reporter.py'
    spec = importlib.util.spec_from_file_location('demo_reporter', path)
    reporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reporter)
    from types import SimpleNamespace
    monkeypatch.setenv('DEMO_RESET_ENABLED', 'true')
    called = []
    monkeypatch.setattr(reporter, 'claim_any_command', lambda *args: called.append('claim'))
    resetter.gate(tmp_path / 'demo-cycles', 'demo_1')
    assert reporter.execute_any(SimpleNamespace(state_dir=str(tmp_path)), 'test') is False
    assert not called
    resetter.gate(tmp_path / 'demo-cycles', 'demo_1', finish=True)
    assert reporter.execute_any(SimpleNamespace(state_dir=str(tmp_path)), 'test') is False
    assert called == ['claim']


def test_cli_reset_failure_keeps_gate_and_retries_same_cycle(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('demo_cli', Path(__file__).parents[1] / 'scripts/demo-cycle.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    from types import SimpleNamespace
    args = SimpleNamespace(cycle='demo_1', state_dir=str(tmp_path), confirm_reset='demo_1')
    demo = module.Demo(args)
    calls = []
    monkeypatch.setattr(demo, 'idle_commands', lambda: None)
    monkeypatch.setattr(demo, 'database', lambda action: calls.append(action) or {'connectors': [], 'databases': {}})
    def control(role, action):
        calls.append((role, action))
        if role == 'destination' and action == 'reset':
            raise RuntimeError('lost response')
    monkeypatch.setattr(demo, 'control', control)
    with pytest.raises(RuntimeError):
        demo.reset()
    assert demo.state['phase'] == 'resetting'
    assert 'begin-reset' in calls and 'finish-reset' not in calls
    assert 'open-source' not in calls
    monkeypatch.setattr(demo, 'control', lambda role, action: calls.append((role, action)))
    assert demo.reset()['phase'] == 'prepared'
    assert calls.index('open-source') > calls.index('reset-db')
    assert calls[-1] == 'finish-reset'


def test_operator_api_supplies_required_origin(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('demo_cli', Path(__file__).parents[1] / 'scripts/demo-cycle.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    from types import SimpleNamespace
    from io import BytesIO
    seen = []
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda request, **kw: seen.append(request) or BytesIO(b'{}'))
    demo = module.Demo(SimpleNamespace(cycle='demo_1', state_dir=str(tmp_path), platform='https://platform.test'))
    demo.api('migrations', {'source_env': 'onprem'})
    assert seen[0].get_header('Origin') == 'https://platform.test'
    assert seen[0].get_method() == 'POST'
