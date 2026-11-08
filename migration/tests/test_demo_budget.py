"""1000건 예산(seed 800 + 복제 중 100 + 전환 후 100)은 옵션이 아니라 고정이다."""
from dataclasses import replace

import pytest

from migration.config import ConfigError, parse_config
from migration.cutover import ApprovalRequired, COMPLETED, CutoverError, VERIFYING
from tests.test_cutover import fakes, _build, _simulate_replication


def budget(config, fakes):
    fakes['db'].seed = {'onprem': 800, 'aws': 0}
    fakes['db'].rows = {'onprem': 800, 'aws': 0}
    _simulate_replication(fakes)
    return config


def test_800_plus_100_plus_100_and_retry_after_target_delivery(config, state_dir, fakes, monkeypatch):
    config = budget(config, fakes)
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
    assert resumed.job.checkpoint['post_switch']['rows'] == {'source': 900, 'target': 1000, 'budget': 1000}
    assert fakes['bot'].generated == 200
    assert fakes['bot'].generation == 'stopped' and fakes['bot'].pending == 0
    assert fakes['db'].rows == {'onprem': 900, 'aws': 1000}
    assert [n for enabled, n in fakes['bot'].calls if enabled] == [100, 200]
    assert not (state_dir / 'requests.sqlite3').exists()


def test_budget_is_fixed_in_configuration(config):
    """100/100 이외의 값과 옛 demo_total_rows 키는 설정 단계에서 거절한다."""
    with pytest.raises(ConfigError, match='1000건 예산'):
        replace(config.cutover, min_live_rows_after_snapshot=3)
    with pytest.raises(ConfigError, match='1000건 예산'):
        replace(config.cutover, min_rows_after_switch=2)
    from tests.conftest import RAW_CONFIG
    with pytest.raises(ConfigError, match='demo_total_rows'):
        parse_config({**RAW_CONFIG, 'cutover': {'demo_total_rows': 1000}})
    assert parse_config(RAW_CONFIG).cutover.min_live_rows_after_snapshot == 100


@pytest.mark.parametrize('problem', ['rows', 'seed', 'target', 'generated', 'running', 'pending', 'failed'])
def test_precheck_refuses_unprepared_data_or_bot(config, state_dir, fakes, problem):
    config = budget(config, fakes)
    if problem == 'rows': fakes['db'].rows['onprem'] = 801
    if problem == 'seed': fakes['db'].seed['onprem'] = 799
    if problem == 'target': fakes['db'].rows['aws'] = 1
    if problem == 'generated': fakes['bot'].generated = 1
    if problem == 'running': fakes['bot'].generation = 'running'
    if problem == 'pending': fakes['bot'].pending = 1
    if problem == 'failed': fakes['bot'].failed = 1
    with pytest.raises(CutoverError, match='1000건 예산'):
        _build(config, state_dir, fakes).run()
    assert fakes['db'].calls == []


def test_source_growth_outside_the_bot_fails_the_budget(config, state_dir, fakes):
    """봇이 아닌 다른 writer 가 원본에 쓰면 900건이 아니라서 READY 전에 멈춘다."""
    config = budget(config, fakes)
    db = fakes['db']
    orig = db.count_rows
    def count_rows(target, phase=None):
        n = orig(target, phase)
        return n + 1 if (target == 'onprem' and phase is None and fakes['bot'].generated == 100) else n
    db.count_rows = count_rows
    with pytest.raises(CutoverError, match='정확히 900건'):
        _build(config, state_dir, fakes).run()
