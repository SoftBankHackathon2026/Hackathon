from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import __main__ as cli
from agent import probe as probe_mod
from agent import restart as restart_mod
from agent import runs as R
from agent.config import ProbeSettings
from agent.probe import PostgresProber
from agent.restart import InClusterKubeApi
from agent.runs import RunStore

CONFIG = {"DB_HOST": "postgres.hackathon.svc.cluster.local", "DB_PORT": "5432", "DB_NAME": "app", "DB_USER": "u"}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def execute(self, sql):
        self.sql = sql

    def fetchone(self):
        return (1,)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict:
    seen: dict = {}

    def create_connection(address, timeout):
        seen["tcp"] = address
        return SimpleNamespace(close=lambda: None)

    def connect(**kwargs):
        seen["connect"] = kwargs
        return FakeConn()

    monkeypatch.setattr(probe_mod.socket, "create_connection", create_connection)
    monkeypatch.setattr(probe_mod.psycopg, "connect", connect)
    return seen


def test_address_map_tunnels_but_keeps_hostname_for_tls(captured: dict) -> None:
    settings = ProbeSettings(address_map={"postgres.hackathon.svc.cluster.local:5432": "127.0.0.1:15432"})
    result = PostgresProber(settings, env={"SAMPLE_APP_DATABASE_URL": "postgresql://u:pw@x/app"}).probe(CONFIG)

    assert result.ok
    assert captured["tcp"] == ("127.0.0.1", 15432)
    kwargs = captured["connect"]
    assert kwargs["host"] == "postgres.hackathon.svc.cluster.local"
    assert kwargs["hostaddr"] == "127.0.0.1"
    assert kwargs["port"] == 15432
    assert kwargs["sslmode"] == "verify-full"
    assert kwargs["password"] == "pw"


def test_unmapped_host_connects_directly(captured: dict) -> None:
    settings = ProbeSettings(address_map={"other:5432": "127.0.0.1:15432"})
    assert PostgresProber(settings, env={}).probe(CONFIG).ok
    assert captured["tcp"] == ("postgres.hackathon.svc.cluster.local", 5432)
    assert "hostaddr" not in captured["connect"]


@pytest.mark.parametrize("bad", ["15432", "127.0.0.1:port"])
def test_invalid_address_map_fails_closed(captured: dict, bad: str) -> None:
    settings = ProbeSettings(address_map={"postgres.hackathon.svc.cluster.local:5432": bad})
    result = PostgresProber(settings, env={}).probe(CONFIG)
    assert (result.ok, result.step, result.error) == (False, "tcp", "AddressMapInvalid")
    assert "tcp" not in captured


def test_kube_api_uses_configured_server_and_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = tmp_path / "token"
    token.write_text("remote-token\n")
    seen: dict = {}
    monkeypatch.setattr(restart_mod.ssl, "create_default_context", lambda cafile: seen.setdefault("ca", cafile))

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    def urlopen(req, timeout, context):
        seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
        return Resp()

    monkeypatch.setattr(restart_mod.urllib.request, "urlopen", urlopen)
    api = InClusterKubeApi(server="https://k8s.example:6443/", token_file=str(token), ca_file=str(tmp_path / "ca.crt"))
    api.get("/api/v1/namespaces/hackathon/pods")

    assert seen["url"] == "https://k8s.example:6443/api/v1/namespaces/hackathon/pods"
    assert seen["auth"] == "Bearer remote-token"
    assert seen["ca"] == str(tmp_path / "ca.crt")


class CountingRemediator:
    def __init__(self, runs: RunStore, state: str = R.NOTHING_TO_FIX) -> None:
        self.runs, self.state, self.calls = runs, state, 0

    def run(self):
        self.calls += 1
        run = self.runs.create("job")
        self.runs.update(run, self.state, "done")
        return run


def test_watch_runs_every_interval(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    remediator = CountingRemediator(runs)
    sleeps: list[float] = []
    cli.watch(remediator, runs, interval=30, iterations=3, sleep=sleeps.append)
    assert remediator.calls == 3
    assert sleeps == [30, 30]
    runs.close()


def test_watch_waits_while_approval_pending(tmp_path: Path) -> None:
    runs = RunStore(tmp_path)
    remediator = CountingRemediator(runs, state=R.WAITING_APPROVAL)
    cli.watch(remediator, runs, interval=1, iterations=3, sleep=lambda _: None)
    assert remediator.calls == 1
    runs.close()


def test_smoke_retries_only_observation_errors() -> None:
    results = iter([{"status": "error"}, {"status": "error"}, {"status": "pass"}])
    sleeps: list[float] = []
    assert cli.smoke_with_retry(lambda: next(results), retries=2, delay=8, sleep=sleeps.append) == {"status": "pass"}
    assert sleeps == [8, 8]
    failed = iter([{"status": "failed"}, {"status": "pass"}])
    assert cli.smoke_with_retry(lambda: next(failed), retries=2, delay=8, sleep=sleeps.append) == {"status": "failed"}
    assert cli.smoke_with_retry(lambda: {"status": "error"}, retries=0, delay=8, sleep=sleeps.append) == {"status": "error"}


def test_probe_retries_after_broken_forward(captured: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def flaky(address, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(probe_mod.socket, "create_connection", flaky)
    sleeps: list[float] = []
    settings = ProbeSettings(attempts=3, retry_delay=5)
    assert PostgresProber(settings, env={}, sleep=sleeps.append).probe(CONFIG).ok
    assert calls["n"] == 2 and sleeps == [5]
    calls["n"] = 0
    assert not PostgresProber(ProbeSettings(attempts=1), env={}, sleep=sleeps.append).probe(CONFIG).ok
    assert calls["n"] == 1


def test_watch_waits_while_approved_run_is_applying(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    runs = RunStore(tmp_path)
    applying = runs.create("job")
    runs.update(applying, R.APPLYING, "승인됨")
    remediator = CountingRemediator(runs)
    cli.watch(remediator, runs, interval=1, iterations=2, sleep=lambda _: None)
    assert remediator.calls == 0
    later = lambda: datetime.now(UTC) + timedelta(seconds=cli.APPLYING_STALE_SECONDS + 60)
    cli.watch(remediator, runs, interval=1, iterations=1, sleep=lambda _: None, now=later)
    assert remediator.calls == 1
    runs.close()
