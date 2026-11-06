import json
import urllib.error
from pathlib import Path

import exporter
import pytest
from exporter import (
    Coefficients,
    CpuTimes,
    KubeWriter,
    Sampler,
    configmap_body,
    energy_delta_joules,
    is_virtual,
    model_watts,
    parse_cpu_times,
    parse_memory_bytes,
    rapl_zones,
    render_metrics,
    utilization,
)

GIB = 2**30
CPUINFO_VM = "processor\t: 0\nflags\t\t: fpu sse2 hypervisor lm\n\nprocessor\t: 1\nflags\t\t: fpu sse2 hypervisor lm\n"
CPUINFO_METAL = "processor\t: 0\nflags\t\t: fpu sse2 lm\n"


def stat(user, nice, system, idle, iowait=0, irq=0, softirq=0, steal=0, guest=0):
    return f"cpu  {user} {nice} {system} {idle} {iowait} {irq} {softirq} {steal} {guest} 0\ncpu0 1 2 3 4\n"


def test_cpu_times_count_steal_and_iowait_as_not_busy():
    times = parse_cpu_times(stat(10, 1, 4, 50, iowait=5, irq=2, softirq=3, steal=7, guest=99))
    assert times == CpuTimes(busy=20, total=82)


def test_cpu_times_accept_short_lines():
    assert parse_cpu_times("cpu 3 0 1 6\n") == CpuTimes(busy=4, total=10)


def test_utilization_is_busy_share_of_elapsed_ticks():
    assert utilization(CpuTimes(10, 100), CpuTimes(40, 200)) == pytest.approx(0.3)


def test_utilization_without_elapsed_ticks_is_zero():
    assert utilization(CpuTimes(10, 100), CpuTimes(10, 100)) == 0.0


def test_utilization_is_clamped():
    assert utilization(CpuTimes(0, 100), CpuTimes(500, 200)) == 1.0
    assert utilization(CpuTimes(50, 100), CpuTimes(10, 200)) == 0.0


def test_memory_total_is_read_in_bytes():
    assert parse_memory_bytes("MemFree: 1 kB\nMemTotal:  4005684 kB\n") == 4005684 * 1024


def test_memory_total_missing_is_an_error():
    with pytest.raises(ValueError):
        parse_memory_bytes("MemFree: 1 kB\n")


def test_hypervisor_flag_marks_a_virtual_machine():
    assert is_virtual(CPUINFO_VM)
    assert not is_virtual(CPUINFO_METAL)
    assert not is_virtual("model name\t: hypervisor-ready cpu\nflags\t: fpu\n")
    assert not is_virtual("flags\t\t: fpu no_hypervisor_hint\n")


def test_model_watts_scales_linearly_between_idle_and_full_load():
    coefficients = Coefficients(
        min_watts_per_vcpu=1, max_watts_per_vcpu=5, watts_per_memory_gib=0.5
    )
    assert model_watts(2, 4 * GIB, 0.0, coefficients) == pytest.approx(4)
    assert model_watts(2, 4 * GIB, 0.5, coefficients) == pytest.approx(8)
    assert model_watts(2, 4 * GIB, 1.0, coefficients) == pytest.approx(12)


def test_model_watts_applies_pue():
    coefficients = Coefficients(
        min_watts_per_vcpu=1, max_watts_per_vcpu=1, watts_per_memory_gib=0, pue=1.5
    )
    assert model_watts(2, GIB, 0.3, coefficients) == pytest.approx(3)


def test_default_coefficients_are_cloud_carbon_footprint_averages():
    assert Coefficients() == Coefficients(0.74, 3.5, 0.392, 1.0)


def test_coefficients_come_from_environment():
    coefficients = Coefficients.from_env({"MAX_WATTS_PER_VCPU": "4", "PUE": "1.2"})
    assert coefficients == Coefficients(0.74, 4.0, 0.392, 1.2)


def test_energy_delta_handles_counter_wrap():
    previous = {"intel-rapl:0": (900, 1000), "intel-rapl:1": (100, 1000)}
    current = {"intel-rapl:0": (100, 1000), "intel-rapl:1": (400, 1000), "intel-rapl:2": (5, 1000)}
    assert energy_delta_joules(previous, current) == pytest.approx(500 / 1_000_000)


def test_rapl_zones_are_packages_only(tmp_path):
    for name in ["intel-rapl:0", "intel-rapl:0:0", "intel-rapl:1", "dtpm"]:
        (tmp_path / name).mkdir()
    assert [z.name for z in rapl_zones(tmp_path)] == ["intel-rapl:0", "intel-rapl:1"]
    assert rapl_zones(tmp_path / "missing") == []


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def make_proc(tmp_path: Path, cpuinfo=CPUINFO_VM, memory_kib=4 * 1024 * 1024) -> Path:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "cpuinfo").write_text(cpuinfo)
    (proc / "meminfo").write_text(f"MemTotal: {memory_kib} kB\n")
    (proc / "stat").write_text(stat(0, 0, 0, 0))
    return proc


def test_sampler_estimates_power_from_cpu_on_a_virtual_machine(tmp_path):
    proc = make_proc(tmp_path)
    clock = Clock()
    coefficients = Coefficients(
        min_watts_per_vcpu=1, max_watts_per_vcpu=3, watts_per_memory_gib=0.5
    )
    sampler = Sampler(
        "k3s-worker", coefficients, proc=proc, powercap=tmp_path / "none", clock=clock
    )
    clock.now += 10
    (proc / "stat").write_text(stat(50, 0, 0, 50))
    first = sampler.sample()
    assert first.method == "model"
    assert first.virtual
    assert first.vcpus == 2
    assert first.cpu_utilization == 0.5
    assert first.power_watts == pytest.approx(2 * 2 + 2)
    assert first.energy_joules == pytest.approx(60)
    clock.now += 30
    (proc / "stat").write_text(stat(50, 0, 0, 150))
    second = sampler.sample()
    assert second.power_watts == pytest.approx(2 * 1 + 2)
    assert second.energy_joules == pytest.approx(60 + 4 * 30)
    assert second.average_watts == pytest.approx(180 / 40)
    assert second.since == "1970-01-01T00:16:40+00:00"
    assert second.updated_at == "1970-01-01T00:17:20+00:00"


def test_sampler_uses_rapl_when_readable(tmp_path):
    proc = make_proc(tmp_path, cpuinfo=CPUINFO_METAL, memory_kib=2 * 1024 * 1024)
    powercap = tmp_path / "powercap"
    zone = powercap / "intel-rapl:0"
    zone.mkdir(parents=True)
    (zone / "max_energy_range_uj").write_text("1000000000")
    (zone / "energy_uj").write_text("0")
    clock = Clock()
    coefficients = Coefficients(watts_per_memory_gib=1)
    sampler = Sampler("metal", coefficients, proc=proc, powercap=powercap, clock=clock)
    clock.now += 10
    (zone / "energy_uj").write_text("150000000")
    reading = sampler.sample()
    assert reading.method == "rapl"
    assert not reading.virtual
    assert reading.power_watts == pytest.approx(15 + 2)


def test_sampler_falls_back_to_model_when_rapl_is_not_readable(tmp_path, monkeypatch):
    proc = make_proc(tmp_path)
    powercap = tmp_path / "powercap"
    (powercap / "intel-rapl:0").mkdir(parents=True)

    attempts = []

    def denied(_zones):
        attempts.append(1)
        raise PermissionError

    monkeypatch.setattr(exporter, "read_energy", denied)
    clock = Clock()
    sampler = Sampler("k3s-master", Coefficients(), proc=proc, powercap=powercap, clock=clock)
    clock.now += 15
    reading = sampler.sample()
    sampler.sample()
    assert reading.method == "model"
    assert reading.rapl_error == "PermissionError"
    assert len(attempts) == 1


def sample_reading(**overrides):
    values = {
        "node": "k3s-master",
        "method": "model",
        "virtual": True,
        "vcpus": 2,
        "memory_bytes": 4 * GIB,
        "cpu_utilization": 0.25,
        "power_watts": 4.5,
        "average_watts": 4.2,
        "energy_joules": 1234.5,
        "since": "2026-11-06T00:00:00+00:00",
        "updated_at": "2026-11-06T01:00:00+00:00",
        "coefficients": Coefficients(),
    }
    values.update(overrides)
    return exporter.Reading(**values)


def test_metrics_are_prometheus_text():
    text = render_metrics(sample_reading())
    assert 'oneport_cost_node_power_watts{node="k3s-master",method="model"} 4.5\n' in text
    assert (
        'oneport_cost_node_energy_joules_total{node="k3s-master",method="model"} 1234.5\n' in text
    )
    assert "# TYPE oneport_cost_node_energy_joules_total counter\n" in text
    assert 'oneport_cost_node_info{node="k3s-master",method="model",virtual="true"} 1\n' in text
    assert render_metrics(None) == ""


def test_configmap_is_named_and_labelled_per_node():
    body = configmap_body("oneport-cost", sample_reading())
    assert body["metadata"]["name"] == "cost-reading-k3s-master"
    assert body["metadata"]["namespace"] == "oneport-cost"
    assert body["metadata"]["labels"]["oneport.io/cost-reading"] == "node"
    data = json.loads(body["data"]["reading.json"])
    assert data["average_watts"] == 4.2
    assert data["coefficients"]["max_watts_per_vcpu"] == 3.5


def test_writer_patches_then_creates_when_missing(monkeypatch):
    calls = []

    def fake(self, method, path, body, content_type):
        calls.append((method, path, content_type))
        if method == "PATCH":
            raise urllib.error.HTTPError(path, 404, "not found", {}, None)

    monkeypatch.setattr(KubeWriter, "_request", fake)
    KubeWriter("oneport-cost").put(sample_reading())
    assert calls == [
        (
            "PATCH",
            "/api/v1/namespaces/oneport-cost/configmaps/cost-reading-k3s-master",
            "application/merge-patch+json",
        ),
        ("POST", "/api/v1/namespaces/oneport-cost/configmaps", "application/json"),
    ]


def test_writer_raises_other_errors(monkeypatch):
    def fake(self, method, path, body, content_type):
        raise urllib.error.HTTPError(path, 403, "forbidden", {}, None)

    monkeypatch.setattr(KubeWriter, "_request", fake)
    with pytest.raises(urllib.error.HTTPError):
        KubeWriter("oneport-cost").put(sample_reading())
