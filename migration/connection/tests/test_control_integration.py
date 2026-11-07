"""Opt-in real PostgreSQL and Redis tests; no production credentials/defaults."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from migration_connection.control.errors import ConnectionError
from migration_connection.control.limits import LimitScope, RedisLimiter
from migration_connection.control.store import PostgresStore
from psycopg import sql
from psycopg.rows import dict_row
from redis import Redis
from test_control import IP, OWNER, approved, envelope, issued, proof
from test_control import svc as control_service_fixture

svc = control_service_fixture


@pytest.fixture
def pg_store():
    dsn = os.getenv("CONNECTION_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("dedicated PostgreSQL not configured")
    schema = "connection_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {} ").format(sql.Identifier(schema)))

    def connect():
        return psycopg.connect(
            dsn, options=f"-csearch_path={schema}", row_factory=dict_row
        )

    with connect() as conn:
        conn.execute(
            (
                Path(__file__).parents[3]
                / "platform/backend/src/controlplane/migrations/014_connections.sql"
            ).read_text()
        )
    try:
        yield PostgresStore(connect)
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def test_postgres_concurrent_proof_atomicity_and_rejected_approval_persistence(
    pg_store, svc
):
    svc.store = pg_store
    request_id = issued(svc)
    body = proof(svc, request_id)

    def attempt(_):
        try:
            return svc.prove(request_id, body)
        except ConnectionError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    success = [r for r in results if r]
    assert len(success) == 1
    for remaining in range(4, -1, -1):
        with pytest.raises(ConnectionError, match="INVALID_CONFIRMATION_CODE"):
            svc.browser(
                request_id,
                OWNER,
                "approve",
                {"candidateDigest": success[0]["candidate_digest"], "code": "wrong"},
            )
        assert svc.get(request_id, OWNER)["attemptsLeft"] == remaining
    assert svc.get(request_id, OWNER)["device"] == "REJECTED"


def test_postgres_create_idempotent_under_concurrency(pg_store, svc):
    svc.store = pg_store
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: issued(svc), range(8)))
    assert len(set(ids)) == 1


def test_postgres_consumes_nonce_on_malformed_enrollment_key(pg_store, svc):
    svc.store = pg_store
    request_id = issued(svc)
    valid = proof(svc, request_id)
    malformed = {**valid, "payload": {**valid["payload"], "wireguard_public_key": []}}
    with pytest.raises(ConnectionError, match="INVALID_PROOF"):
        svc.prove(request_id, malformed)
    with pytest.raises(ConnectionError, match="INVALID_CHALLENGE"):
        svc.prove(request_id, valid)
    assert svc.prove(request_id, proof(svc, request_id))["device_id"]


def test_postgres_consumes_nonce_on_malformed_signed_device_payload(pg_store, svc):
    svc.store = pg_store
    request_id = approved(svc)
    valid = envelope(svc, request_id, "status", {})
    malformed = {
        **valid,
        "payload": {**valid["payload"], "data": {"out_of_range": 2**53}},
    }
    with pytest.raises(ConnectionError, match="INVALID_DEVICE_REQUEST"):
        svc.device(request_id, malformed)
    with pytest.raises(ConnectionError, match="INVALID_CHALLENGE"):
        svc.device(request_id, valid)
    assert svc.device(request_id, envelope(svc, request_id, "status", {}))["device_id"]


@pytest.fixture
def redis_limiter():
    url = os.getenv("CONNECTION_TEST_REDIS_URL")
    if not url:
        pytest.skip("dedicated Redis not configured")
    client = Redis.from_url(url, socket_timeout=2)
    client.flushdb()
    try:
        yield RedisLimiter(client)
    finally:
        client.flushdb()
        client.close()


def test_redis_twentieth_failure_blocks_24h_across_korean_midnight(redis_limiter):
    now = int(
        datetime(
            2026, 11, 7, 23, 59, 0, tzinfo=timezone(timedelta(hours=9))
        ).timestamp()
        * 1000
    )
    for _ in range(19):
        assert redis_limiter.fail(IP, now) == 0
    redis_limiter.check(IP, now)
    assert redis_limiter.fail(IP, now) == now + 86400000
    with pytest.raises(ConnectionError, match="AUTHENTICATION_BLOCKED"):
        redis_limiter.check(IP, now + 120000)
    redis_limiter.check(IP, now + 86400001)


def test_redis_failures_atomic_and_separate_ipv6_normalized(redis_limiter):
    now = 1800000000000
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(
            pool.map(lambda _: redis_limiter.fail("2001:db8::1", now), range(20))
        )
    assert sum(bool(value) for value in results) == 1
    with pytest.raises(ConnectionError, match="AUTHENTICATION_BLOCKED"):
        redis_limiter.check("2001:0db8:0:0:0:0:0:1", now)
    redis_limiter.check("2001:db8::2", now)


def test_redis_runtime_expiry_and_release_binding(redis_limiter):
    now = 1800000000000
    assert not redis_limiter.runtime_ready("v1", "hash", now)
    redis_limiter.record_runtime("v1", "hash", now)
    assert redis_limiter.runtime_ready("v1", "hash", now + 1)
    assert not redis_limiter.runtime_ready("v2", "hash", now + 1)
    assert not redis_limiter.runtime_ready("v1", "hash", now + 60000)


def test_redis_scopes_isolate_request_blocks_and_account_rate_limits(redis_limiter):
    now = 1800000000000
    request = LimitScope("enrollment", ("first",))
    for _ in range(20):
        redis_limiter.fail(request, now)
    with pytest.raises(ConnectionError, match="AUTHENTICATION_BLOCKED"):
        redis_limiter.check(request, now)
    redis_limiter.check(LimitScope("enrollment", ("second",)), now)
    redis_limiter.check(LimitScope("device", ("first",)), now)
    redis_limiter.check(IP, now)
    account = LimitScope("browser", ("team", "a@example.test"))
    for _ in range(120):
        redis_limiter.check(account, now)
    with pytest.raises(ConnectionError, match="REQUEST_RATE_LIMITED"):
        redis_limiter.check(account, now)
    redis_limiter.check(LimitScope("browser", ("team", "b@example.test")), now)


def test_redis_unavailable_never_allows_registration():
    limiter = RedisLimiter(
        Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.1)
    )
    assert not limiter.ready()
    with pytest.raises(ConnectionError, match="REQUEST_LIMIT_UNAVAILABLE"):
        limiter.check(IP, 1800000000000)
