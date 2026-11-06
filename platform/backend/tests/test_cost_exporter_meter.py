import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from controlplane.cost.collect import collect_inventory
from controlplane.cost.config import OnPremSpec
from controlplane.cost.estimator import price_onprem
from controlplane.cost.meters import cost_exporter
from controlplane.cost.meters.cost_exporter import (
    CostExporterMeter,
    NoCostReadings,
    StaleCostReadings,
)
from controlplane.cost.onprem import create_meter, registered
from controlplane.integrations.defaults import ONPREM

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def reading(node, average, current=None, *, age=30, method="model", virtual=True):
    return {
        "reading.json": json.dumps(
            {
                "node": node,
                "method": method,
                "virtual": virtual,
                "average_watts": average,
                "power_watts": current if current is not None else average,
                "updated_at": (NOW - timedelta(seconds=age)).isoformat(),
            }
        )
    }


def meter(items, **options):
    seen = []

    def lister(env, namespace):
        seen.append((env.id, namespace))
        return items

    return CostExporterMeter(options, lister=lister, clock=lambda: NOW), seen


def test_meter_is_registered_under_its_config_name():
    assert "cost-exporter" in registered()
    assert isinstance(create_meter("cost-exporter", {}), CostExporterMeter)


def test_meter_sums_average_power_of_fresh_nodes():
    m, seen = meter([reading("k3s-worker", 9.25, 20, age=90), reading("k3s-master", 4.2, 3)])
    result = m.read(ONPREM)
    assert seen == [("onprem", "oneport-cost")]
    assert result.source == "cost-exporter"
    assert result.power_watts == 13.4
    assert result.observed_at == NOW - timedelta(seconds=90)
    assert result.note == "노드 2대, VM 2대, CPU 사용률 기반 추정"
    assert result.hardware_usd is None and result.electricity_usd_per_kwh is None


def test_meter_can_use_current_power_and_another_namespace():
    m, seen = meter([reading("a", 5, 11), reading("b", 5, 2)], use="current", namespace="cost")
    assert m.read(ONPREM).power_watts == 13
    assert seen == [("onprem", "cost")]


def test_meter_drops_stale_nodes_and_says_so():
    m, _ = meter(
        [reading("a", 5, age=30), reading("b", 7, age=601), reading("c", 1, age=600)],
        max_age_seconds=600,
    )
    result = m.read(ONPREM)
    assert result.power_watts == 6
    assert result.note == "노드 2대, VM 2대, CPU 사용률 기반 추정, 오래된 값 1개 제외"


def test_meter_names_each_method_and_bare_metal_nodes():
    m, _ = meter([reading("a", 5, method="rapl", virtual=False), reading("b", 5)])
    assert m.read(ONPREM).note == "노드 2대, VM 1대, CPU 사용률 기반 추정, RAPL 전력 측정"


def test_meter_ignores_broken_configmaps():
    broken = [{}, {"reading.json": "{"}, {"reading.json": json.dumps({"node": "x"})}]
    m, _ = meter([*broken, reading("a", 3)])
    assert m.read(ONPREM).power_watts == 3


def test_meter_without_readings_is_a_gap():
    m, _ = meter([{}])
    with pytest.raises(NoCostReadings):
        m.read(ONPREM)
    inv = collect_inventory(ONPREM, {}, kube=lambda env: ([], [], []), meter=m)
    assert inv.gaps == ["온프렘 측정기: 읽기 실패 (NoCostReadings)"]


def test_meter_with_only_stale_readings_is_a_gap():
    m, _ = meter([reading("a", 3, age=3600)])
    with pytest.raises(StaleCostReadings):
        m.read(ONPREM)


def test_measured_power_feeds_the_electricity_line_with_its_note():
    m, _ = meter([reading("a", 10), reading("b", 10)])
    cost = price_onprem(OnPremSpec(electricity_usd_per_kwh=0.1), m.read(ONPREM))
    power = next(line for line in cost.lines if line.item == "전기")
    assert power.basis == "measured"
    assert power.quantity == 20
    assert power.monthly == round(20 / 1000 * 730 * 0.1, 2)
    assert power.note == "측정: cost-exporter (노드 2대, VM 2대, CPU 사용률 기반 추정)"


def test_default_lister_reads_labelled_configmaps(monkeypatch):
    calls = []

    class FakeCore:
        def __init__(self, api):
            calls.append(("api", api))

        def list_namespaced_config_map(self, namespace, label_selector):
            calls.append((namespace, label_selector))
            return SimpleNamespace(
                items=[SimpleNamespace(data={"k": "v"}), SimpleNamespace(data=None)]
            )

    monkeypatch.setattr(cost_exporter, "api_client", lambda env: f"client:{env.id}")
    monkeypatch.setattr(cost_exporter.client, "CoreV1Api", FakeCore)
    assert cost_exporter.list_readings(ONPREM, "oneport-cost") == [{"k": "v"}, {}]
    assert calls == [("api", "client:onprem"), ("oneport-cost", "oneport.io/cost-reading=node")]
