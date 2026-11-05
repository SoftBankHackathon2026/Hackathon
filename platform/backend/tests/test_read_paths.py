"""Monitoring and history reads stay cheap under polling."""

import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from controlplane.domain.models import Runtime
from controlplane.providers.contracts import ProviderUnavailable
from controlplane.providers.onprem.telemetry import connections
from controlplane.services.read_cache import ReadCache


def test_read_cache_reuses_fresh_results_and_expires():
    async def scenario():
        now = [0.0]
        cache = ReadCache(clock=lambda: now[0])
        compute = AsyncMock(side_effect=["first", "second"])
        assert await cache.get("k", 10, compute) == "first"
        now[0] = 9
        assert await cache.get("k", 10, compute) == "first"
        now[0] = 11
        assert await cache.get("k", 10, compute) == "second"
        assert compute.call_count == 2

    asyncio.run(scenario())


def test_read_cache_drops_expired_results_and_stays_bounded():
    async def scenario():
        now = [0.0]
        cache = ReadCache(max_entries=3, clock=lambda: now[0])
        for key in "abc":
            await cache.get(key, 10, AsyncMock(return_value=key))
        now[0] = 11
        await cache.get("d", 10, AsyncMock(return_value="d"))
        assert list(cache._entries) == ["d"]
        for key in "efg":
            await cache.get(key, 10, AsyncMock(return_value=key))
        assert len(cache._entries) <= 3

    asyncio.run(scenario())


def test_forgotten_reads_are_neither_served_nor_stored():
    async def scenario():
        cache = ReadCache()
        release = asyncio.Event()

        async def old():
            await release.wait()
            return "old app"

        await cache.get(("traffic", "env"), 10, AsyncMock(return_value="cached"))
        await cache.get(("traffic", "other"), 10, AsyncMock(return_value="kept"))
        running = asyncio.ensure_future(cache.get(("traces", "env"), 10, old))
        await asyncio.sleep(0)
        cache.forget(lambda key: key[1] == "env")
        fresh = AsyncMock(return_value="new app")
        assert await cache.get(("traffic", "env"), 10, fresh) == "new app"
        assert await cache.get(("traces", "env"), 10, fresh) == "new app"
        release.set()
        assert await running == "old app"
        assert await cache.get(("traces", "env"), 10, AsyncMock()) == "new app"
        assert await cache.get(("traffic", "other"), 10, AsyncMock()) == "kept"

    asyncio.run(scenario())


def test_read_cache_keeps_neither_failures_nor_missing_results():
    async def scenario():
        cache = ReadCache()
        compute = AsyncMock(side_effect=[ProviderUnavailable("down"), None, "ok"])
        with pytest.raises(ProviderUnavailable):
            await cache.get("k", 10, compute)
        assert await cache.get("k", 10, compute) is None
        assert await cache.get("k", 10, compute) == "ok"

    asyncio.run(scenario())


def test_concurrent_identical_reads_share_one_call_and_its_error():
    async def scenario():
        cache = ReadCache()
        release = asyncio.Event()
        calls = []

        async def slow(result):
            calls.append(result)
            await release.wait()
            if isinstance(result, Exception):
                raise result
            return result

        for outcome in ("value", ProviderUnavailable("down")):
            release.clear()
            readers = [
                asyncio.ensure_future(cache.get(str(outcome), 10, lambda: slow(outcome)))
                for _ in range(4)
            ]
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(*readers, return_exceptions=True)
            assert results == [outcome] * 4
        assert len(calls) == 2

    asyncio.run(scenario())


def test_shared_read_survives_the_request_that_started_it():
    async def scenario():
        cache = ReadCache()
        release = asyncio.Event()

        async def slow():
            await release.wait()
            return "value"

        owner = asyncio.ensure_future(cache.get("k", 10, slow))
        waiter = asyncio.ensure_future(cache.get("k", 10, slow))
        await asyncio.sleep(0)
        owner.cancel()
        release.set()
        assert await waiter == "value"
        assert await cache.get("k", 10, AsyncMock(side_effect=AssertionError)) == "value"

    asyncio.run(scenario())


def test_monitoring_route_shares_the_read_but_checks_access_every_time(store, monkeypatch):
    from fastapi.testclient import TestClient

    from controlplane.core.errors import OperationNotFound
    from controlplane.main import create_app
    from controlplane.providers.registry import ProviderRegistry
    from controlplane.providers.telemetry import TraceList
    from controlplane.services.deployments import DeploymentService

    reader = Mock()
    reader.traces.return_value = TraceList(
        application="notes",
        environment_id="env",
        minutes=30,
        kind="all",
        tracing_enabled=True,
        traces=[],
    )
    reader.trace.return_value = None
    monkeypatch.setattr(DeploymentService, "telemetry", lambda *args: reader)
    with TestClient(create_app(registry=ProviderRegistry(), store=store, auth_mode="demo")) as api:
        assert [api.get("/api/v1/environments/env/traces").status_code for _ in range(3)] == [
            200
        ] * 3
        assert api.get("/api/v1/environments/env/traces?kind=errors").status_code == 200
        assert reader.traces.call_count == 2
        trace_id = "a" * 32
        assert [
            api.get(f"/api/v1/environments/env/traces/{trace_id}").status_code for _ in range(2)
        ] == [404, 404]
        assert reader.trace.call_count == 2

        def denied(*args):
            raise OperationNotFound("없음")

        monkeypatch.setattr(DeploymentService, "telemetry", denied)
        assert api.get("/api/v1/environments/env/traces").status_code == 404


def enqueue(store, application, team):
    request = NS(
        application=application,
        environment_id=application + "-env",
        model_dump=lambda mode: {"application": application},
    )
    return store.enqueue(
        request, application + "-request-key", "binding", runtime=Runtime.AWS_ECS, team=team
    )


def test_history_with_events_matches_single_release_reads(store):
    first = enqueue(store, "alpha", "red")
    second = enqueue(store, "beta", "blue")
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO release_events(release_id,state,message) VALUES(%s,'running','started')",
            (first["id"],),
        )
    assert store.list_with_events() == [store.get(second["id"]), store.get(first["id"])]
    assert [r["id"] for r in store.list_with_events(team="red")] == [first["id"]]
    assert store.list_with_events(team="nobody") == []


def test_release_events_are_indexed_by_release(store):
    with store.connect() as conn:
        index = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname='release_events_by_release'"
            " AND schemaname=current_schema()"
        ).fetchone()
    assert index and "(release_id, id)" in index["indexdef"]


def test_history_filters_environment_and_team_before_paging(store):
    first = enqueue(store, "alpha", "red")
    second = enqueue(store, "beta", "red")
    foreign = enqueue(store, "gamma", "blue")
    enqueue(store, "newest", "red")
    with store.connect() as conn:
        conn.execute(
            "UPDATE releases SET environment_id='alpha-env' WHERE id=ANY(%s)",
            ([second["id"], foreign["id"]],),
        )
    assert [
        r["id"] for r in store.list_with_events(team="red", environment_id="alpha-env", limit=1)
    ] == [second["id"]]
    assert [
        r["id"]
        for r in store.list_with_events(team="red", environment_id="alpha-env", limit=1, offset=1)
    ] == [first["id"]]
    assert store.list_with_events(team="red", environment_id="alpha-env", limit=1, offset=2) == []
    assert store.list_with_events(team="nobody", environment_id="alpha-env") == []


def test_release_history_page_size_is_bounded(store):
    from fastapi.testclient import TestClient

    from controlplane.main import create_app
    from controlplane.providers.registry import ProviderRegistry

    with TestClient(create_app(registry=ProviderRegistry(), store=store, auth_mode="demo")) as api:
        assert api.get("/api/v1/releases?limit=101").status_code == 422
        assert api.get("/api/v1/releases?offset=-1").status_code == 422


def onprem_settings(server):
    return NS(kubeconfig="/kubeconfig", context="ctx", server=server)


def test_onprem_monitoring_shares_clients_per_cluster_and_reconnects(monkeypatch):
    connect = Mock(side_effect=lambda **kwargs: Mock(name=kwargs["expected_server"]))
    monkeypatch.setattr(connections, "connect", connect)
    monkeypatch.setattr(connections, "_connections", {})
    now = [0.0]
    monkeypatch.setattr(connections.time, "monotonic", lambda: now[0])
    one, other = onprem_settings("https://one"), onprem_settings("https://two")
    clients = connections.shared_clients(one)
    assert connections.shared_clients(onprem_settings("https://one")) is clients
    assert connections.shared_clients(other) is not clients
    assert connect.call_count == 2
    connections.forget_clients(one)
    assert connections.shared_clients(one) is not clients
    now[0] = connections.CONNECTION_TTL + 1
    connections.shared_clients(other)
    assert connect.call_count == 4


def test_pooled_connections_are_reused_and_come_back_clean(store):
    from controlplane.core import db

    def backend():
        with store.connect() as conn:
            return conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]

    assert backend() != backend()
    with db.pooling(max_size=2):
        with store.connect() as conn:
            first = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            conn.execute("SELECT pg_advisory_lock(424242)")
            conn.execute("CREATE TABLE pool_probe(id int)")
        with pytest.raises(RuntimeError), store.connect() as conn:
            conn.execute("INSERT INTO pool_probe VALUES(1)")
            raise RuntimeError("rolled back")
        assert backend() == first
        with store.connect() as conn:
            assert conn.execute("SELECT count(*) AS n FROM pool_probe").fetchone()["n"] == 0
            held = conn.execute(
                "SELECT count(*) AS n FROM pg_locks WHERE locktype='advisory' AND objid=424242"
            ).fetchone()["n"]
            assert held == 0
    assert not db._pools and backend() != first
