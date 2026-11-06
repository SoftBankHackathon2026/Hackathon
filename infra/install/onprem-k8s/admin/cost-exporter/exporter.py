import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = logging.getLogger("oneport-cost-exporter")
LABEL = "oneport.io/cost-reading"
DATA_KEY = "reading.json"


@dataclass(frozen=True)
class Coefficients:
    min_watts_per_vcpu: float = 0.74
    max_watts_per_vcpu: float = 3.5
    watts_per_memory_gib: float = 0.392
    pue: float = 1.0

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "Coefficients":
        base = cls()
        return cls(
            min_watts_per_vcpu=float(env.get("MIN_WATTS_PER_VCPU", base.min_watts_per_vcpu)),
            max_watts_per_vcpu=float(env.get("MAX_WATTS_PER_VCPU", base.max_watts_per_vcpu)),
            watts_per_memory_gib=float(env.get("WATTS_PER_MEMORY_GIB", base.watts_per_memory_gib)),
            pue=float(env.get("PUE", base.pue)),
        )


@dataclass(frozen=True)
class CpuTimes:
    busy: int
    total: int


@dataclass(frozen=True)
class Reading:
    node: str
    method: str
    virtual: bool
    vcpus: int
    memory_bytes: int
    cpu_utilization: float
    power_watts: float
    average_watts: float
    energy_joules: float
    since: str
    updated_at: str
    coefficients: Coefficients
    rapl_error: str | None = None


def parse_cpu_times(stat: str) -> CpuTimes:
    values = [int(v) for v in stat.splitlines()[0].split()[1:]]
    values += [0] * (8 - len(values))
    user, nice, system, idle, iowait, irq, softirq, steal = values[:8]
    busy = user + nice + system + irq + softirq
    return CpuTimes(busy=busy, total=busy + idle + iowait + steal)


def utilization(previous: CpuTimes, current: CpuTimes) -> float:
    span = current.total - previous.total
    if span <= 0:
        return 0.0
    return min(1.0, max(0.0, (current.busy - previous.busy) / span))


def parse_memory_bytes(meminfo: str) -> int:
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    raise ValueError("MemTotal 없음")


def is_virtual(cpuinfo: str) -> bool:
    for line in cpuinfo.splitlines():
        if line.startswith("flags") and "hypervisor" in line.split(":", 1)[1].split():
            return True
    return False


def count_vcpus(cpuinfo: str) -> int:
    return sum(1 for line in cpuinfo.splitlines() if line.startswith("processor")) or 1


def memory_watts(memory_bytes: int, coefficients: Coefficients) -> float:
    return memory_bytes / 2**30 * coefficients.watts_per_memory_gib


def model_watts(vcpus: int, memory_bytes: int, cpu: float, coefficients: Coefficients) -> float:
    span = coefficients.max_watts_per_vcpu - coefficients.min_watts_per_vcpu
    per_vcpu = coefficients.min_watts_per_vcpu + span * cpu
    return (vcpus * per_vcpu + memory_watts(memory_bytes, coefficients)) * coefficients.pue


def rapl_zones(powercap: Path) -> list[Path]:
    if not powercap.is_dir():
        return []
    return sorted(
        zone
        for zone in powercap.iterdir()
        if zone.name.startswith("intel-rapl:") and zone.name.count(":") == 1
    )


def read_energy(zones: list[Path]) -> dict[str, tuple[int, int]]:
    return {
        zone.name: (
            int((zone / "energy_uj").read_text()),
            int((zone / "max_energy_range_uj").read_text()),
        )
        for zone in zones
    }


def energy_delta_joules(
    previous: dict[str, tuple[int, int]], current: dict[str, tuple[int, int]]
) -> float:
    total = 0
    for name, (now, limit) in current.items():
        if name not in previous:
            continue
        before = previous[name][0]
        total += now - before if now >= before else limit - before + now
    return total / 1_000_000


class Sampler:
    def __init__(
        self,
        node: str,
        coefficients: Coefficients,
        proc: Path = Path("/proc"),
        powercap: Path = Path("/sys/class/powercap"),
        clock=time.time,
    ) -> None:
        self._node = node
        self._coefficients = coefficients
        self._proc = proc
        self._clock = clock
        cpuinfo = (proc / "cpuinfo").read_text()
        self._virtual = is_virtual(cpuinfo)
        self._vcpus = count_vcpus(cpuinfo)
        self._zones = rapl_zones(powercap)
        self._rapl_error: str | None = None
        self._started = clock()
        self._at = self._started
        self._cpu = self._cpu_times()
        self._energy = self._read_rapl()
        self._joules = 0.0

    def _cpu_times(self) -> CpuTimes:
        return parse_cpu_times((self._proc / "stat").read_text())

    def _read_rapl(self) -> dict[str, tuple[int, int]] | None:
        if not self._zones:
            return None
        try:
            return read_energy(self._zones)
        except OSError as exc:
            self._rapl_error = type(exc).__name__
            self._zones = []
            return None

    def sample(self) -> Reading:
        now = self._clock()
        seconds = max(now - self._at, 1e-9)
        cpu_now = self._cpu_times()
        cpu = utilization(self._cpu, cpu_now)
        memory = parse_memory_bytes((self._proc / "meminfo").read_text())
        energy_now = self._read_rapl()
        if energy_now is not None and self._energy is not None:
            method = "rapl"
            package = energy_delta_joules(self._energy, energy_now) / seconds
            watts = (package + memory_watts(memory, self._coefficients)) * self._coefficients.pue
        else:
            method = "model"
            watts = model_watts(self._vcpus, memory, cpu, self._coefficients)
        self._joules += watts * seconds
        self._at, self._cpu, self._energy = now, cpu_now, energy_now
        elapsed = max(now - self._started, 1e-9)
        return Reading(
            node=self._node,
            method=method,
            virtual=self._virtual,
            vcpus=self._vcpus,
            memory_bytes=memory,
            cpu_utilization=round(cpu, 4),
            power_watts=round(watts, 2),
            average_watts=round(self._joules / elapsed, 2),
            energy_joules=round(self._joules, 1),
            since=_iso(self._started),
            updated_at=_iso(now),
            coefficients=self._coefficients,
            rapl_error=self._rapl_error,
        )


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def render_metrics(reading: Reading | None) -> str:
    if reading is None:
        return ""
    labels = f'node="{reading.node}",method="{reading.method}"'
    rows = [
        ("oneport_cost_node_power_watts", "gauge", reading.power_watts),
        ("oneport_cost_node_average_power_watts", "gauge", reading.average_watts),
        ("oneport_cost_node_energy_joules_total", "counter", reading.energy_joules),
        ("oneport_cost_node_cpu_utilization_ratio", "gauge", reading.cpu_utilization),
        ("oneport_cost_node_vcpus", "gauge", reading.vcpus),
        ("oneport_cost_node_memory_bytes", "gauge", reading.memory_bytes),
    ]
    lines = []
    for name, kind, value in rows:
        lines += [f"# TYPE {name} {kind}", f"{name}{{{labels}}} {value}"]
    virtual = "true" if reading.virtual else "false"
    lines += [
        "# TYPE oneport_cost_node_info gauge",
        f'oneport_cost_node_info{{{labels},virtual="{virtual}"}} 1',
    ]
    return "\n".join(lines) + "\n"


def configmap_name(node: str) -> str:
    return f"cost-reading-{node}"


def configmap_body(namespace: str, reading: Reading) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": configmap_name(reading.node),
            "namespace": namespace,
            "labels": {LABEL: "node", "app.kubernetes.io/name": "oneport-cost-exporter"},
        },
        "data": {DATA_KEY: json.dumps(asdict(reading), ensure_ascii=False, sort_keys=True)},
    }


@dataclass
class KubeWriter:
    namespace: str
    server: str = "https://kubernetes.default.svc"
    account: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    _context: ssl.SSLContext | None = field(default=None, init=False, repr=False)

    def _request(self, method: str, path: str, body: dict, content_type: str) -> None:
        if self._context is None:
            self._context = ssl.create_default_context(cafile=str(self.account / "ca.crt"))
        token = (self.account / "token").read_text().strip()
        request = urllib.request.Request(
            self.server + path,
            data=json.dumps(body).encode(),
            method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
        )
        with urllib.request.urlopen(request, context=self._context, timeout=10):
            pass

    def put(self, reading: Reading) -> None:
        body = configmap_body(self.namespace, reading)
        base = f"/api/v1/namespaces/{self.namespace}/configmaps"
        try:
            self._request(
                "PATCH",
                f"{base}/{body['metadata']['name']}",
                body,
                "application/merge-patch+json",
            )
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            self._request("POST", base, body, "application/json")


class State:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reading: Reading | None = None

    def set(self, reading: Reading) -> None:
        with self._lock:
            self._reading = reading

    def get(self) -> Reading | None:
        with self._lock:
            return self._reading


def handler_for(state: State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/metrics":
                body, status = render_metrics(state.get()).encode(), 200
            elif self.path == "/healthz":
                body, status = (b"ok", 200) if state.get() else (b"warming up", 503)
            else:
                body, status = b"not found", 404
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            return

    return Handler


def run(env: dict[str, str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    node = env["NODE_NAME"]
    interval = float(env.get("SAMPLE_SECONDS", "15"))
    publish_every = float(env.get("PUBLISH_SECONDS", "60"))
    sampler = Sampler(node, Coefficients.from_env(env), proc=Path(env.get("PROC_ROOT", "/proc")))
    writer = KubeWriter(namespace=env["POD_NAMESPACE"])
    state = State()
    server = ThreadingHTTPServer(("0.0.0.0", int(env.get("PORT", "9877"))), handler_for(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    published = 0.0
    while True:
        time.sleep(interval)
        reading = sampler.sample()
        state.set(reading)
        if time.time() - published >= publish_every:
            try:
                writer.put(reading)
                published = time.time()
            except (OSError, urllib.error.URLError) as exc:
                LOG.warning("node=%s publish failed: %s", node, type(exc).__name__)


if __name__ == "__main__":
    run(dict(os.environ))
