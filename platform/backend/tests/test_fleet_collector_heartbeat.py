import contextlib
from types import SimpleNamespace

import pytest

from controlplane.execution import fleet_collector


@pytest.fixture
def ticks(monkeypatch):
    """main 을 DB·클러스터 없이 돌린다. tick 마다 호출된 함수를 부른다."""
    calls = []

    class Collector:
        def __init__(self, *args, **kwargs):
            pass

        def tick(self):
            for call in calls:
                if callable(call):
                    call()

    store = SimpleNamespace(migrate=lambda: None)
    monkeypatch.setattr(
        fleet_collector, "load_fleet_config", lambda: SimpleNamespace(sample_seconds=1)
    )
    monkeypatch.setattr(fleet_collector, "load_platform", lambda: SimpleNamespace(store=store))
    monkeypatch.setattr(fleet_collector, "load_cost_config", lambda: None)
    monkeypatch.setattr(fleet_collector, "FleetCollector", Collector)
    monkeypatch.setattr(fleet_collector, "FleetJournal", lambda store: None)
    monkeypatch.setattr(fleet_collector, "CostStore", lambda store: None)
    monkeypatch.setattr(fleet_collector.db, "pooling", contextlib.nullcontext)
    monkeypatch.setattr(fleet_collector, "MigrationTaskJournal", lambda store: None)
    monkeypatch.setattr(
        fleet_collector,
        "MigrationTaskService",
        lambda config, journal: SimpleNamespace(sync=lambda: calls.append("sync") or None),
    )
    return calls


def test_heartbeat_file_is_touched_before_each_tick(tmp_path, monkeypatch, ticks):
    heartbeat = tmp_path / "collector-heartbeat"
    seen = []
    ticks.append(lambda: seen.append(heartbeat.exists()))
    monkeypatch.setenv(fleet_collector.HEARTBEAT_ENV, str(heartbeat))

    assert fleet_collector.main(["--once"]) == 0
    assert seen == [True]


def test_no_heartbeat_without_env(tmp_path, monkeypatch, ticks):
    monkeypatch.delenv(fleet_collector.HEARTBEAT_ENV, raising=False)
    monkeypatch.chdir(tmp_path)

    assert fleet_collector.main(["--once"]) == 0
    assert list(tmp_path.iterdir()) == []


def test_saved_tasks_are_loaded_before_each_tick(monkeypatch, ticks):
    monkeypatch.delenv(fleet_collector.HEARTBEAT_ENV, raising=False)
    seen = []
    ticks.append(lambda: seen.append(list(ticks)))

    assert fleet_collector.main(["--once"]) == 0
    assert seen and "sync" in seen[0]
