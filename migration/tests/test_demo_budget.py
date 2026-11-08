"""Bounded HTTP-bot batches replace the retired DB worker in demo orchestration."""
from dataclasses import replace

import pytest

from migration.config import ConfigError, parse_config
from migration.cutover import ApprovalRequired, COMPLETED, CutoverError, VERIFYING
from tests.test_cutover import fakes, _build, _simulate_replication


def demo(config, fakes):
    config = replace(config, cutover=replace(config.cutover, demo_total_rows=1000,
                     min_live_rows_after_snapshot=100, min_rows_after_switch=100))
    fakes['db'].seed = {'onprem': 800, 'aws': 0}
    fakes['db'].rows = {'onprem': 800, 'aws': 0}
    _simulate_replication(fakes)
    return config


def test_800_plus_100_plus_100_and_retry_after_target_delivery(config, state_dir, fakes, monkeypatch):
    config = demo(config, fakes)
    first = _build(config, state_dir, fakes)
    with pytest.raises(ApprovalRequired):
        first.run()
    assert fakes['db'].rows == {'onprem': 900, 'aws': 900}
    assert fakes['bot'].generated == 100 and fakes['bot'].generation == 'stopped'
    second = _build(config, state_dir, fakes, approve='test')
    original_save = second._save
    def crash(stage=None, **checkpoint):
        if stage == VERIFYING:
            raise RuntimeError('crash after target delivery')
        original_save(stage, **checkpoint)
    monkeypatch.setattr(second, '_save', crash)
    with pytest.raises(CutoverError, match='crash after target delivery'):
        second.run()
    resumed = _build(config, state_dir, fakes, approve='test')
    assert resumed.run(force_retry=True) == COMPLETED
    assert resumed.job.checkpoint['target_rows_before'] == 900
    assert fakes['bot'].generated == 200
    assert fakes['bot'].generation == 'stopped' and fakes['bot'].pending == 0
    assert fakes['db'].rows == {'onprem': 900, 'aws': 1000}
    assert [n for enabled, n in fakes['bot'].calls if enabled] == [100, 200]
    assert not (state_dir / 'requests.sqlite3').exists()


def test_demo_configuration_rejects_invalid_budget(config):
    with pytest.raises(ConfigError):
        replace(config.cutover, demo_total_rows=1000)
    with pytest.raises(ConfigError):
        replace(config.cutover, demo_total_rows=1001, min_live_rows_after_snapshot=100, min_rows_after_switch=100)


@pytest.mark.parametrize('value', [None, 1000])
def test_yaml_demo_option(config, value):
    from tests.conftest import RAW_CONFIG
    raw = {**RAW_CONFIG, 'cutover': {'demo_total_rows': value}}
    assert parse_config(raw).cutover.demo_total_rows == value


@pytest.mark.parametrize('problem', ['rows', 'generated', 'running', 'pending'])
def test_demo_refuses_unprepared_data_or_bot(config, state_dir, fakes, problem):
    config = demo(config, fakes)
    if problem == 'rows': fakes['db'].rows['onprem'] = 801
    if problem == 'generated': fakes['bot'].generated = 1
    if problem == 'running': fakes['bot'].generation = 'running'
    if problem == 'pending': fakes['bot'].pending = 1
    with pytest.raises(CutoverError, match='1000-row demo'):
        _build(config, state_dir, fakes).run()
    assert fakes['db'].calls == []
