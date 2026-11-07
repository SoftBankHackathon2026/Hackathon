"""Security state-machine tests with explicit crypto/store/clock test doubles."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from migration_connection.contracts.protocol import b64, canonical, signed_bytes
from migration_connection.control.errors import ConnectionError
from migration_connection.control.service import Actor, ConnectionService
from migration_connection.control.store import MemoryStore
from migration_connection.inventory import Kubectl, collect, discover
from test_inventory import FakeKubectl

PUBLIC = b64(b"e" * 1952)
DEVICE = b64(b"d" * 1952)
WG = b64(b"w" * 32)
OWNER = Actor("team", "owner@example.test", True)
VIEWER = Actor("team", "viewer@example.test", False)
OTHER = Actor("other", "owner@example.test", True)
IP = "192.0.2.1"
CANDIDATE = "candidate_1234567890"


class Crypto:
    def verify(self, public, signature, message):
        return signature == self.signature(public, message)

    def signature(self, public, message):
        return hashlib.sha384(public.encode() + message).hexdigest()

    def sign(self, message):
        return self.signature("job-public", message)

    def ready(self):
        return True


class Limiter:
    def __init__(self):
        self.failures_by_scope = {}
        self.runtime = True

    @property
    def failures(self):
        return sum(self.failures_by_scope.values())

    def check(self, scope, now):
        if self.failures_by_scope.get(scope, 0) >= 20:
            raise ConnectionError("AUTHENTICATION_BLOCKED", 429)

    def fail(self, scope, now):
        self.failures_by_scope[scope] = self.failures_by_scope.get(scope, 0) + 1

    def ready(self):
        return True

    def runtime_ready(self, version, bootstrap, now, tenant=""):
        return self.runtime

    def record_runtime(self, version, bootstrap, now, tenant=""):
        self.runtime = True


@pytest.fixture
def svc():
    now = [1800000000000]
    instance = ConnectionService(
        MemoryStore(),
        Crypto(),
        Limiter(),
        bootstrap_url="https://connect.example.test/i",
        release_version="test-1",
        runtime_token="r" * 32,
        bootstrap_sha384="a" * 96,
        runtime_tenant_id="team",
        runtime_gateway_id="gateway-test",
        clock=lambda: now[0],
    )
    instance.test_now = now
    return instance


def issued(svc):
    return svc.create(OWNER, PUBLIC, "operation_1234567890")["id"]


def proof(svc, request_id):
    challenge = svc.challenge(request_id, CANDIDATE)
    p = {
        "version": 1,
        "request_id": request_id,
        "candidate_id": CANDIDATE,
        **challenge,
        "device_auth_public_key": DEVICE,
        "wireguard_public_key": WG,
        "audience": "migration-connector-enrollment",
    }
    return {
        "payload": p,
        "enrollment_signature": svc.crypto.signature(
            PUBLIC, signed_bytes("enrollment", p)
        ),
        "device_signature": svc.crypto.signature(
            DEVICE, signed_bytes("device-possession", p)
        ),
        "metadata": {"hostname": "host", "os": "Ubuntu", "arch": "arm64"},
    }


def approved(svc):
    request_id = issued(svc)
    result = svc.prove(request_id, proof(svc, request_id))
    svc.browser(
        request_id,
        OWNER,
        "approve",
        {
            "candidateDigest": result["candidate_digest"],
            "code": result["confirmation_code"],
        },
    )
    return request_id


def ready(svc):
    request_id = approved(svc)
    svc.tunnel(
        request_id,
        {
            "epoch": 1,
            "connection_id": "connection_1234567890",
            "state": "READY",
            "checks": 2,
            "key_expires_at_ms": svc.clock() + 1800000,
            "lease_expires_at_ms": svc.clock() + 60000,
        },
    )
    return request_id


def envelope(svc, request_id, action, data):
    challenge = svc.challenge(request_id, CANDIDATE, device=True)
    p = {
        "version": 1,
        "request_id": request_id,
        "candidate_id": CANDIDATE,
        **challenge,
        "action": action,
        "data": data,
    }
    return {
        "payload": p,
        "signature": svc.crypto.signature(DEVICE, signed_bytes("device-request", p)),
    }


def report(svc, request_id):
    row = svc.store.get(request_id)
    client = Kubectl(runner=FakeKubectl(), executable="/usr/local/bin/kubectl")
    binding = {
        "request_id": request_id,
        "device_id": row["device_id"],
        "epoch": row["epoch"],
    }
    found = discover(client, **binding)["discovered"]
    svc.device(
        request_id,
        envelope(svc, request_id, "discovered", {"environments": found}),
        through_tunnel=True,
    )
    target = next(e["id"] for e in found if e["name"] == "prod")
    svc.browser(request_id, OWNER, "collect", {"discoveredId": target})
    task = svc.store.get(request_id)["task"]["payload"]
    value = collect(
        client, selected_target_id=target, clock=lambda: svc.clock() / 1000, **binding
    )
    return task, value


def test_idempotent_creation_and_collision(svc):
    request_id = issued(svc)
    assert issued(svc) == request_id
    with pytest.raises(ConnectionError, match="IDEMPOTENCY_CONFLICT"):
        svc.create(OWNER, DEVICE, "operation_1234567890")
    assert len(request_id) == 22


def test_capability_requires_runtime_lease(svc):
    svc.limiter.runtime = False
    assert not svc.capabilities(OWNER.team)["available"]
    with pytest.raises(ConnectionError, match="UNAVAILABLE"):
        issued(svc)


def test_registration_expiry_server_time(svc):
    request_id = issued(svc)
    body = proof(svc, request_id)
    svc.test_now[0] += 60000
    with pytest.raises(ConnectionError, match="ENROLLMENT_UNAVAILABLE"):
        svc.prove(request_id, body)
    assert svc.limiter.failures == 0
    assert svc.get(request_id, OWNER)["registration"] == "EXPIRED"


def test_bad_signature_consumes_challenge_but_not_enrollment(svc):
    request_id = issued(svc)
    body = proof(svc, request_id)
    body["device_signature"] = "bad"
    with pytest.raises(ConnectionError, match="INVALID_PROOF"):
        svc.prove(request_id, body)
    assert svc.store.get(request_id)["registration"] == "ISSUED"
    assert svc.store.get(request_id)["challenges"] == {}
    assert svc.limiter.failures == 1
    assert svc.prove(request_id, proof(svc, request_id))["device_id"]


def test_concurrent_proof_only_one_consumes_request(svc):
    request_id = issued(svc)
    body = proof(svc, request_id)

    def attempt(_):
        try:
            svc.prove(request_id, body)
            return True
        except ConnectionError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 1
    assert svc.limiter.failures == 0


def test_snapshot_never_contains_public_proof_or_confirmation_secret(svc):
    request_id = approved(svc)
    serialized = json.dumps(svc.get(request_id, OWNER))
    assert (
        PUBLIC not in serialized and DEVICE not in serialized and WG not in serialized
    )
    assert svc.store.get(request_id)["code"] not in serialized


def test_approval_failure_counter_persists_and_rejects(svc):
    request_id = issued(svc)
    result = svc.prove(request_id, proof(svc, request_id))
    for left in range(4, -1, -1):
        with pytest.raises(ConnectionError, match="INVALID_CONFIRMATION_CODE"):
            svc.browser(
                request_id,
                OWNER,
                "approve",
                {"candidateDigest": result["candidate_digest"], "code": "WRONG"},
            )
        assert svc.get(request_id, OWNER)["attemptsLeft"] == left
    assert svc.get(request_id, OWNER)["device"] == "REJECTED"


def test_cross_team_and_viewer_cannot_approve(svc):
    request_id = issued(svc)
    result = svc.prove(request_id, proof(svc, request_id))
    body = {
        "candidateDigest": result["candidate_digest"],
        "code": result["confirmation_code"],
    }
    for actor in (OTHER, VIEWER):
        with pytest.raises(ConnectionError):
            svc.browser(request_id, actor, "approve", body)
    assert svc.get(request_id, OWNER)["device"] == "PENDING_APPROVAL"


def test_response_loss_status_recovery_only_same_device(svc):
    request_id = issued(svc)
    result = svc.prove(request_id, proof(svc, request_id))
    body = envelope(svc, request_id, "status", {})
    recovered = svc.device(request_id, body)
    assert recovered["device_id"] == result["device_id"]
    assert recovered["confirmation_code"] == result["confirmation_code"]
    with pytest.raises(ConnectionError, match="INVALID_CHALLENGE"):
        svc.device(request_id, body)


def test_approval_expiry(svc):
    request_id = issued(svc)
    result = svc.prove(request_id, proof(svc, request_id))
    svc.test_now[0] += 300000
    with pytest.raises(ConnectionError, match="APPROVAL_UNAVAILABLE"):
        svc.browser(
            request_id,
            OWNER,
            "approve",
            {
                "candidateDigest": result["candidate_digest"],
                "code": result["confirmation_code"],
            },
        )


def test_public_http_cannot_submit_inventory_even_with_signature(svc):
    request_id = ready(svc)
    with pytest.raises(ConnectionError, match="TUNNEL_TRANSPORT_REQUIRED"):
        svc.device(request_id, envelope(svc, request_id, "poll", {}))


def test_lease_and_key_expiration_block_tasks(svc):
    request_id = ready(svc)
    svc.test_now[0] += 60000
    assert svc.get(request_id, OWNER)["tunnel"] == "DEGRADED"
    with pytest.raises(ConnectionError, match="TUNNEL_NOT_READY"):
        svc.device(
            request_id, envelope(svc, request_id, "poll", {}), through_tunnel=True
        )


def test_heartbeat_cannot_extend_key_lifetime(svc):
    request_id = ready(svc)
    svc.test_now[0] += 10000
    with pytest.raises(ConnectionError, match="KEY_LIFETIME_CHANGED"):
        svc.tunnel(
            request_id,
            {
                "epoch": 1,
                "connection_id": "connection_1234567890",
                "state": "READY",
                "checks": 2,
                "key_expires_at_ms": svc.clock() + 1800000,
                "lease_expires_at_ms": svc.clock() + 60000,
            },
        )


def test_register_only_valid_selected_inventory_and_duplicate_rejected(svc):
    request_id = ready(svc)
    task, value = report(svc, request_id)
    svc.device(
        request_id,
        envelope(
            svc, request_id, "inventory", {"job_id": task["job_id"], "report": value}
        ),
        through_tunnel=True,
    )
    snapshot = svc.get(request_id, OWNER)
    target = {
        "organization": OWNER.team,
        "project": "project",
        "name": "production",
        "kind": "kubernetes",
        "role": "source",
        "region": "",
    }
    data = {
        "collectedId": value["target_id"],
        "inventoryDigest": snapshot["inventoryDigest"],
        "target": target,
    }
    bad = deepcopy(data)
    bad["target"]["organization"] = "other"
    with pytest.raises(ConnectionError, match="INVALID_REGISTRATION"):
        svc.browser(request_id, OWNER, "register", bad)
    result = svc.browser(request_id, OWNER, "register", data)
    assert result["environmentId"]
    assert result["workloads"]["items"][0]["name"] == "actual-app"
    assert svc.list(OWNER)[0]["environmentId"] == result["environmentId"]
    with pytest.raises(ConnectionError, match="ALREADY_REGISTERED"):
        svc.browser(request_id, OWNER, "register", data)
    with pytest.raises(ConnectionError, match="ALREADY_REGISTERED"):
        svc.browser(request_id, OWNER, "collect", {"discoveredId": "different-cluster"})
    refreshed = svc.browser(
        request_id, OWNER, "collect", {"discoveredId": value["target_id"]}
    )
    assert refreshed["environmentId"] == result["environmentId"]
    assert refreshed["collectionPending"]
    assert refreshed["workloads"] is None


def test_rekey_discards_inflight_report(svc):
    request_id = ready(svc)
    task, value = report(svc, request_id)
    svc.tunnel(
        request_id,
        {
            "epoch": 2,
            "connection_id": "connection_2234567890",
            "state": "READY",
            "checks": 2,
            "key_expires_at_ms": svc.clock() + 1800000,
            "lease_expires_at_ms": svc.clock() + 60000,
        },
    )
    with pytest.raises(ConnectionError, match="STALE_COLLECTION"):
        svc.device(
            request_id,
            envelope(
                svc,
                request_id,
                "inventory",
                {"job_id": task["job_id"], "report": value},
            ),
            through_tunnel=True,
        )


def test_revoke_is_immediate_and_cleanup_separate(svc):
    request_id = ready(svc)
    svc.browser(request_id, OWNER, "revoke", {})
    with pytest.raises(ConnectionError, match="DEVICE_NOT_APPROVED"):
        svc.authorize(request_id)
    assert svc.get(request_id, OWNER)["cleanup"] == {
        "server": True,
        "gateway": False,
        "resources": True,
        "local": False,
    }
    svc.tunnel(request_id, {"state": "REVOKED"})
    svc.device(request_id, envelope(svc, request_id, "cleanup", {"local": True}))
    assert all(svc.get(request_id, OWNER)["cleanup"].values())


def test_device_key_expires_after_90_days(svc):
    request_id = ready(svc)
    svc.test_now[0] += 90 * 86400000
    with pytest.raises(ConnectionError, match="DEVICE_NOT_APPROVED"):
        svc.authorize(request_id)
    assert svc.get(request_id, OWNER)["device"] == "REVOKED"


def test_canonical_subset_rejects_floats_and_orders_utf16():
    assert canonical({"b": 2, "a": "한글"}) == '{"a":"한글","b":2}'.encode()
    for value in (1.1, 2**60, "\ud800"):
        with pytest.raises((ValueError, UnicodeError)):
            canonical(value)


def test_runtime_token_cannot_cross_tenant_even_for_relay_status(svc):
    request_id = approved(svc)
    body = envelope(svc, request_id, "status", {})
    svc.runtime_tenant_id = "other"
    for call in (
        lambda: svc.authorize(request_id),
        lambda: svc.tunnel(request_id, {"state": "REVOKED"}),
        lambda: svc.device(request_id, body, through_tunnel=True),
    ):
        with pytest.raises(ConnectionError, match="RUNTIME_TENANT_MISMATCH"):
            call()
    assert svc.device(request_id, body)["snapshot"]["device"] == "APPROVED"


@pytest.mark.parametrize("elapsed,accepted", [(90000, True), (120000, False)])
def test_collection_task_120_seconds_independent_of_lease(svc, elapsed, accepted):
    request_id = ready(svc)
    task, value = report(svc, request_id)
    assert task["expires_at_ms"] == svc.clock() + 120000
    key_expiry = svc.store.get(request_id)["key_expires_at"]
    svc.test_now[0] += elapsed
    svc.tunnel(
        request_id,
        {
            "epoch": 1,
            "connection_id": "connection_1234567890",
            "state": "READY",
            "checks": 2,
            "key_expires_at_ms": key_expiry,
            "lease_expires_at_ms": svc.clock() + 60000,
        },
    )
    value["collected_at_ms"] = svc.clock()
    body = envelope(
        svc, request_id, "inventory", {"job_id": task["job_id"], "report": value}
    )
    if accepted:
        assert svc.device(request_id, body, through_tunnel=True)["accepted"]
    else:
        with pytest.raises(ConnectionError, match="STALE_COLLECTION"):
            svc.device(request_id, body, through_tunnel=True)


@pytest.mark.parametrize("expired", [False, True])
def test_rejected_candidate_can_only_recover_status_and_report_cleanup(svc, expired):
    request_id = issued(svc)
    svc.prove(request_id, proof(svc, request_id))
    if expired:
        svc.test_now[0] += 300000
    else:
        svc.browser(request_id, OWNER, "reject", {})
    status = svc.device(request_id, envelope(svc, request_id, "status", {}))
    assert status["snapshot"]["device"] == "REJECTED"
    assert status["confirmation_code"] is None
    with pytest.raises(ConnectionError, match="TUNNEL_NOT_READY"):
        svc.device(
            request_id, envelope(svc, request_id, "poll", {}), through_tunnel=True
        )
    assert svc.device(
        request_id, envelope(svc, request_id, "cleanup", {"local": True})
    )["accepted"]
    assert svc.get(request_id, OWNER)["cleanup"]["local"]


def test_rekey_ready_binds_expiry_to_new_epoch_but_heartbeat_cannot_extend_it(svc):
    request_id = ready(svc)
    first_expiry = svc.store.get(request_id)["key_expires_at"]
    svc.test_now[0] += 1000
    svc.tunnel(
        request_id,
        {
            "epoch": 2,
            "connection_id": "second_connection_1234",
            "state": "REKEYING",
        },
    )
    second_expiry = svc.clock() + 1800000
    assert second_expiry != first_expiry
    state = {
        "epoch": 2,
        "connection_id": "second_connection_1234",
        "state": "READY",
        "checks": 2,
        "key_expires_at_ms": second_expiry,
        "lease_expires_at_ms": svc.clock() + 60000,
    }
    svc.tunnel(request_id, state)
    assert svc.get(request_id, OWNER)["tunnel"] == "READY"
    svc.test_now[0] += 1000
    svc.tunnel(request_id, {**state, "lease_expires_at_ms": svc.clock() + 60000})
    with pytest.raises(ConnectionError, match="KEY_LIFETIME_CHANGED"):
        svc.tunnel(request_id, {**state, "key_expires_at_ms": second_expiry + 1000})
    svc.tunnel(
        request_id,
        {
            "epoch": 2,
            "connection_id": "second_connection_1234",
            "state": "REKEYING",
        },
    )
    with pytest.raises(ConnectionError, match="KEY_LIFETIME_CHANGED"):
        svc.tunnel(request_id, {**state, "key_expires_at_ms": second_expiry + 1000})


def test_offline_reconnect_times_out_and_requires_a_new_tunnel(svc):
    request_id = ready(svc)
    initial_activity = svc.get(request_id, OWNER)["lastTunnelActivityAt"]
    svc.test_now[0] += 10_000
    svc.device(request_id, envelope(svc, request_id, "poll", {}), through_tunnel=True)
    last_activity = svc.clock()
    assert last_activity > initial_activity

    svc.test_now[0] += 60_000
    offline = svc.get(request_id, OWNER)
    assert offline["tunnel"] == "DEGRADED"
    assert offline["lastTunnelActivityAt"] == last_activity
    reconnect = svc.browser(request_id, OWNER, "reconnect", {})
    assert reconnect["tunnel"] == "CONNECTING"
    assert reconnect["tunnelChecks"] == 0
    svc.test_now[0] += 30_000
    duplicate = svc.browser(request_id, OWNER, "reconnect", {})
    assert duplicate["revision"] == reconnect["revision"]
    stale_report = {
        "epoch": 1,
        "connection_id": "connection_1234567890",
        "state": "READY",
        "checks": 2,
        "key_expires_at_ms": svc.store.get(request_id)["key_expires_at"],
        "lease_expires_at_ms": svc.clock() + 60_000,
    }
    with pytest.raises(ConnectionError, match="NEW_KEY_EPOCH_REQUIRED"):
        svc.tunnel(request_id, stale_report)

    svc.test_now[0] += 60_000
    timed_out = svc.get(request_id, OWNER)
    assert timed_out["tunnel"] == "DEGRADED"
    assert timed_out["lastTunnelActivityAt"] == last_activity
    assert "단말 응답을 확인하지 못했습니다" in timed_out["issue"]
    assert svc.browser(request_id, OWNER, "reconnect", {})["tunnel"] == "CONNECTING"
    svc.tunnel(
        request_id,
        {
            **stale_report,
            "epoch": 2,
            "connection_id": "second_connection_1234",
            "lease_expires_at_ms": svc.clock() + 60_000,
        },
    )
    restored = svc.get(request_id, OWNER)
    assert restored["tunnel"] == "READY"
    assert restored["epoch"] == 2
    assert restored["lastTunnelActivityAt"] == svc.clock()
