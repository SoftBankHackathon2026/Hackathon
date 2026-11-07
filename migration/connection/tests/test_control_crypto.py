"""Real ML-DSA interoperability; explicit OpenSSL path enables local verification.

OpenSSL 3.6 may verify test vectors here. Production readiness remains pinned to 3.5.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from migration_connection.contracts.protocol import b64
from migration_connection.control.crypto import MLDSA65_SPKI_PREFIX, OpenSSLCrypto


@pytest.fixture
def real_crypto(tmp_path):
    binary = os.getenv("CONNECTION_TEST_OPENSSL")
    if not binary:
        pytest.skip("real ML-DSA OpenSSL not configured")
    key = tmp_path / "job-key.pem"
    subprocess.run(
        [binary, "genpkey", "-algorithm", "ML-DSA-65", "-out", str(key)],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    return OpenSSLCrypto(binary, str(key))


def test_browser_noble_signature_verified_by_python_openssl(real_crypto):
    fixture = json.loads(
        (
            Path(__file__).parents[1] / "agent/tests/fixtures/noble-mldsa65.json"
        ).read_text()
    )
    assert real_crypto.verify(
        fixture["public_key"], fixture["signature"], fixture["message"].encode()
    )
    assert not real_crypto.verify(
        fixture["public_key"],
        fixture["signature"],
        b"wrong-domain\n" + fixture["message"].encode(),
    )


def test_native_signature_verified_by_python_openssl(real_crypto):
    executable = os.getenv("CONNECTION_TEST_NATIVE")
    if not executable:
        pytest.skip("native connector executable not configured")
    vector = json.loads(
        subprocess.run(
            [executable, "crypto-vector"], check=True, capture_output=True
        ).stdout
    )
    assert real_crypto.verify(
        vector["public_key"], vector["signature"], vector["message"].encode()
    )


def test_server_job_signing_and_raw_public_key_format(real_crypto):
    der = subprocess.run(
        [
            real_crypto.binary,
            "pkey",
            "-in",
            real_crypto.signing_key,
            "-pubout",
            "-outform",
            "DER",
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert der.startswith(MLDSA65_SPKI_PREFIX)
    public = b64(der[len(MLDSA65_SPKI_PREFIX) :])
    message = b'migration/job/v1\n{"operation":"environment.inspect"}'
    signature = real_crypto.sign(message)
    assert real_crypto.verify(public, signature, message)
    assert not real_crypto.verify(public, signature, message + b" ")


@pytest.fixture
def native_signer():
    executable = os.getenv("CONNECTION_TEST_SIGNER")
    if not executable:
        pytest.skip("test-only native signer not configured")

    def sign(purpose, payload, seed=7):
        return json.loads(
            subprocess.run(
                [executable],
                input=json.dumps(
                    {
                        "purpose": "migration/" + purpose + "/v1",
                        "payload": payload,
                        "seed": seed,
                    }
                ).encode(),
                check=True,
                capture_output=True,
            ).stdout
        )

    return sign


# Explicit fixture sharing, without changing the production readiness gate.
from test_control import svc as control_service_fixture
from test_control_api import client as control_client_fixture

svc = control_service_fixture
client = control_client_fixture


def test_native_enrollment_and_signed_device_http_roundtrip(
    client, svc, real_crypto, native_signer
):
    enrollment_public = native_signer("enrollment", {}, 7)["public_key"]
    device_public = native_signer("device-request", {}, 8)["public_key"]
    response = client.post(
        "/api/v1/connections",
        json={"publicKey": enrollment_public, "operationId": "native_roundtrip_1234"},
    )
    assert response.status_code == 201
    request_id = response.json()["id"]
    # The deployment gate above is an explicit fake; all protocol verification below
    # uses real OpenSSL. This test does not assert a production runtime is ready.
    svc.crypto = real_crypto
    candidate_id = "native_candidate_1234"
    challenge = client.post(
        f"/api/v1/connections/{request_id}/challenge",
        json={"candidateId": candidate_id},
    ).json()
    payload = {
        "version": 1,
        "request_id": request_id,
        "candidate_id": candidate_id,
        **challenge,
        "device_auth_public_key": device_public,
        "wireguard_public_key": b64(b"w" * 32),
        "audience": "migration-connector-enrollment",
    }
    result = client.post(
        f"/api/v1/connections/{request_id}/prove",
        json={
            "payload": payload,
            "enrollment_signature": native_signer("enrollment", payload, 7)[
                "signature"
            ],
            "device_signature": native_signer("device-possession", payload, 8)[
                "signature"
            ],
        },
    )
    assert result.status_code == 200, result.text
    proof = result.json()
    challenge = client.post(
        f"/api/v1/connections/{request_id}/device-challenge",
        json={"candidateId": candidate_id},
    ).json()
    payload = {
        "version": 1,
        "request_id": request_id,
        "candidate_id": candidate_id,
        **challenge,
        "action": "status",
        "data": {},
    }
    body = {
        "payload": payload,
        "signature": native_signer("device-request", payload, 8)["signature"],
    }
    recovered = client.post(f"/api/v1/connections/{request_id}/recover", json=body)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["confirmation_code"] == proof["confirmation_code"]
    assert (
        client.post(
            f"/api/v1/connections/{request_id}/approve",
            json={
                "candidateDigest": proof["candidate_digest"],
                "code": proof["confirmation_code"],
            },
        ).status_code
        == 200
    )
    headers = {"Authorization": "Bearer " + svc.runtime_token}
    authorized = client.get(
        f"/api/v1/connection-runtime/{request_id}/authorize", headers=headers
    )
    assert authorized.json()["device_auth_public_key"] == device_public
    assert (
        client.post(
            f"/api/v1/connection-runtime/{request_id}/tunnel",
            headers=headers,
            json={
                "epoch": 1,
                "connection_id": "native_connection_1234",
                "state": "READY",
                "checks": 2,
                "key_expires_at_ms": svc.clock() + 1800000,
                "lease_expires_at_ms": svc.clock() + 60000,
            },
        ).status_code
        == 200
    )
    challenge = client.post(
        f"/api/v1/connections/{request_id}/device-challenge",
        json={"candidateId": candidate_id},
    ).json()
    payload = {
        "version": 1,
        "request_id": request_id,
        "candidate_id": candidate_id,
        **challenge,
        "action": "poll",
        "data": {},
    }
    body = {
        "payload": payload,
        "signature": native_signer("device-request", payload, 8)["signature"],
    }
    poll = client.post(
        f"/api/v1/connection-runtime/{request_id}/device", headers=headers, json=body
    )
    assert poll.status_code == 200, poll.text
    assert poll.json()["snapshot"]["tunnel"] == "READY"
    replay = client.post(
        f"/api/v1/connection-runtime/{request_id}/device", headers=headers, json=body
    )
    assert replay.status_code == 401
