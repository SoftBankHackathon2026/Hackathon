from controlplane.domain.fleet import FleetConfig
from controlplane.execution.fleet_collector import COST_SECONDS, FACTS_SECONDS, FleetCollector
from controlplane.integrations.defaults import AWS


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def scheduled():
    clock = Clock()
    runs = []
    collector = FleetCollector(
        FleetConfig(mode="live", environments=[AWS], migrations=[]),
        journal=None,
        clock=clock,
    )
    collector.collect_environment = lambda env: runs.append(("facts", clock.now))
    collector.collect_costs = lambda env: runs.append(("costs", clock.now))
    return clock, collector, runs


def test_first_tick_reads_facts_and_costs():
    clock, collector, runs = scheduled()
    collector.tick()
    assert runs == [("facts", 1000.0), ("costs", 1000.0)]


def test_facts_and_costs_follow_wall_clock_intervals():
    clock, collector, runs = scheduled()
    collector.tick()
    runs.clear()
    clock.now += FACTS_SECONDS - 1
    collector.tick()
    assert runs == []
    clock.now += 1
    collector.tick()
    assert runs == [("facts", 1000.0 + FACTS_SECONDS)]
    runs.clear()
    clock.now = 1000.0 + COST_SECONDS - 1
    collector.tick()
    assert [name for name, _ in runs] == ["facts"]
    runs.clear()
    clock.now = 1000.0 + COST_SECONDS
    collector.tick()
    assert runs == [("costs", 1000.0 + COST_SECONDS)]


def test_slow_ticks_still_collect_costs_every_interval():
    clock, collector, runs = scheduled()
    for _ in range(5):
        collector.tick()
        clock.now += COST_SECONDS / 2
    assert [at for name, at in runs if name == "costs"] == [
        1000.0,
        1000.0 + COST_SECONDS,
        1000.0 + 2 * COST_SECONDS,
    ]


def test_many_fast_ticks_do_not_collect_costs_early():
    clock, collector, runs = scheduled()
    for _ in range(100):
        collector.tick()
        clock.now += 1
    assert [at for name, at in runs if name == "costs"] == [1000.0]


def test_costs_are_read_every_five_minutes_and_facts_every_minute():
    clock, collector, runs = scheduled()
    for _ in range(601):
        collector.tick()
        clock.now += 1
    assert [at for name, at in runs if name == "facts"] == [1000.0 + 60 * k for k in range(11)]
    assert [at for name, at in runs if name == "costs"] == [1000.0, 1300.0, 1600.0]
