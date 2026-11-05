"""Console rollback to an earlier version: deploy #1, deploy #2, then restore #1."""

from fastapi.testclient import TestClient

from controlplane.main import create_app
from controlplane.providers.registry import ProviderRegistry
from oneport_aws.adapter import AwsAdapter

from .test_worker import enqueue, harness, run  # noqa: F401

TOKEN = "restore-test-operator-token-0123456789"
HEADERS = {"Authorization": "Bearer " + TOKEN}


def deploy(h, request, key):
    row = enqueue(h, request, key)
    return finish(h, row["id"])


def finish(h, release_id):
    run(h, release_id, {"waiting"})
    h.aws.advance("shop", "IN_PROGRESS", "BAKE_TIME")
    row = run(h, release_id, {"succeeded", "failed", "uncertain"})
    assert row["state"] == "succeeded", row["events"]
    return row


def client(h):
    registry = ProviderRegistry()
    registry.register(AwsAdapter(h.settings))
    return TestClient(create_app(registry=registry, store=h.store, operator_token=TOKEN))


def test_restore_redeploys_the_first_release_revision_after_two_successes(harness):  # noqa: F811
    h = harness
    first = deploy(h, h.make_request(), "restore-first-key-0001")
    second = deploy(h, h.make_request(), "restore-second-key-001")
    assert h.aws.services["shop"]["taskDefinition"].endswith("oneport-shop:2")

    with client(h) as c:
        env = next(e for e in c.get("/api/v1/environments").json() if e["id"] == "aws-oneport")
        assert env["release_restore"] is True
        response = c.post(
            f"/api/v1/releases/{first['id']}/restore",
            json={"idempotency_key": "restore-first-again-01"},
            headers=HEADERS,
        )
        assert response.status_code == 202, response.text
        restored = response.json()
        assert restored["restore_of"] == first["id"] and restored["id"] not in {
            first["id"],
            second["id"],
        }

    row = finish(h, restored["id"])
    # The first release's own revision is reused: same image, env and secrets.
    assert h.aws.services["shop"]["taskDefinition"].endswith("oneport-shop:1")
    assert any("revision" in e["message"] for e in row["events"])
    with client(h) as c:
        listed = {r["id"]: r for r in c.get("/api/v1/releases", headers=HEADERS).json()}
    assert listed[restored["id"]]["restore_of"] == first["id"]
    assert listed[second["id"]]["restore_of"] is None


def test_only_succeeded_releases_of_this_app_can_be_restored(harness):  # noqa: F811
    h = harness
    first = deploy(h, h.make_request(), "restore-only-ok-key-01")
    active = enqueue(h, h.make_request(), "restore-active-key-001")
    with client(h) as c:
        busy = c.post(
            f"/api/v1/releases/{first['id']}/restore",
            json={"idempotency_key": "restore-while-busy-001"},
            headers=HEADERS,
        )
        assert busy.status_code == 409 and "진행 중" in busy.json()["detail"]
        not_done = c.post(
            f"/api/v1/releases/{active['id']}/restore",
            json={"idempotency_key": "restore-unfinished-001"},
            headers=HEADERS,
        )
        assert not_done.status_code == 409 and "성공" in not_done.json()["detail"]
        missing = c.post(
            "/api/v1/releases/" + "f" * 24 + "/restore",
            json={"idempotency_key": "restore-missing-key-01"},
            headers=HEADERS,
        )
        assert missing.status_code == 404


def test_a_release_made_for_another_registration_is_refused_in_korean(harness):  # noqa: F811
    h = harness
    first = deploy(h, h.make_request(), "restore-mismatch-key-01")
    adapter = AwsAdapter(h.settings)
    adapter.register_application(port=9090, health_path="/healthz")
    with client(h) as c:
        response = c.post(
            f"/api/v1/releases/{first['id']}/restore",
            json={"idempotency_key": "restore-mismatch-key-02"},
            headers=HEADERS,
        )
    assert response.status_code == 409 and "다시 쓸 수 없어" in response.json()["detail"]
