"""Real PostgreSQL checkpoints plus fake upstream; no cloud credentials or mutations."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from controlplane.domain.models import Runtime
from controlplane.execution.gcp_bridge.metadata import PHASES, public_metadata
from controlplane.execution.gcp_bridge.worker import GcpRuntime, GcpWorker
from controlplane.main import create_app
from controlplane.providers.gcp_bridge.adapter import GcpAdapter
from controlplane.providers.gcp_bridge.models import GcpRequest, GcpSettings
from controlplane.providers.registry import ProviderRegistry

REPO = "https://github.com/example/demo.git"
URL = "https://demo-example-du.a.run.app"
REMOTE = "a" * 32
IMAGE = "asia-northeast3-docker.pkg.dev/project-example/oneport-apps/sample@sha256:" + "b" * 64
TOKEN = "bridge-fake-operator-token-0123456789012345"
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def settings(store):
    return GcpSettings(
        environment_id="gcp-demo",
        database_url=store.dsn,
        allowed_repositories=[REPO],
        service_url=URL,
        service_access="public",
    )


def result(**changes):
    return {
        "deployment_id": REMOTE,
        "provider": "gcp",
        "app_id": "demo",
        "repository_url": REPO,
        "status": "SUCCESS",
        "url": URL,
        "revision": "demo-cb-new",
        "image_uri": IMAGE,
        "health": "OK",
        "project_id": "project-example",
        "source_commit": "c" * 40,
    } | changes


def enqueue(store, settings):
    return store.enqueue(
        GcpRequest(environment_id="gcp-demo", source={"repository": REPO}),
        "gcp-bluegreen-regression-01",
        settings.fingerprint(),
        runtime=Runtime.GCP_CLOUD_RUN,
    )


def tick(worker):
    # Advance only the local journal's polling clock; never change the upstream.
    with worker.store.connect() as conn:
        conn.execute("UPDATE releases SET next_attempt_at=now()")
    worker.tick()


def test_post_intent_committed_before_side_effect_and_restart_polls_only(store, settings):
    row = enqueue(store, settings)
    calls = []

    def upstream(request):
        calls.append(request.method)
        if request.method == "POST":
            saved = store.get(row["id"])
            assert saved["state"] == "remote_submitting"
            assert saved["checkpoint"]["submission_intent"] is True
            assert json.loads(request.content)["strategy"] == "blue_green"
            return httpx.Response(202, json={"deployment_id": REMOTE})
        return httpx.Response(200, json=result(status="PENDING"))

    runtime = GcpRuntime(settings, transport=httpx.MockTransport(upstream))
    try:
        tick(GcpWorker(store, runtime))
        assert store.get(row["id"])["checkpoint"]["remote_id"] == REMOTE
        runtime.close()
        runtime = GcpRuntime(settings, transport=httpx.MockTransport(upstream))
        for _ in range(3):
            tick(GcpWorker(store, runtime))
        assert calls == ["POST", "GET", "GET", "GET"]
        assert store.get(row["id"])["state"] == "queued"  # upstream PENDING, with saved id
    finally:
        runtime.close()


def test_post_promotion_failure_restores_but_release_remains_failed(store, settings):
    stages = ["CHECKING", "RESTORING", "RESTORE_CHECKING", "FAILED"]
    calls, history = [], []

    def upstream(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(202, json={"deployment_id": REMOTE})
        status = stages.pop(0)
        history.append({"status": status, "message": "never-publish-this"})
        return httpx.Response(
            200,
            json=result(
                status=status,
                history=history,
                rollback_status="SUCCESS" if status == "FAILED" else None,
                previous_revision="demo-cb-old",
                active_revision="demo-cb-old",
                candidate_revision="demo-cb-failed",
                error={"private_key": "never-publish-this"},
                logs="never-publish-this",
            ),
        )

    registry = ProviderRegistry()
    registry.register(GcpAdapter(settings))
    runtime = GcpRuntime(settings, transport=httpx.MockTransport(upstream))
    try:
        with TestClient(create_app(registry=registry, store=store, operator_token=TOKEN)) as client:
            payload = {
                "request": GcpRequest(
                    environment_id="gcp-demo", source={"repository": REPO}
                ).model_dump(mode="json"),
                "idempotency_key": "gcp-rollback-api-regression-01",
            }
            response = client.post("/api/v1/releases", json=payload, headers=HEADERS)
            assert response.status_code == 202
            rid = response.json()["id"]
            assert (
                client.post("/api/v1/releases", json=payload, headers=HEADERS).json()["id"] == rid
            )
            worker = GcpWorker(store, runtime)
            for _ in range(5):
                tick(worker)
            detail = client.get(f"/api/v1/releases/{rid}", headers=HEADERS).json()
            assert detail["status"] == "failed" and detail["rollback_status"] == "SUCCESS"
            assert detail["previous_revision"] == detail["active_revision"] == "demo-cb-old"
            assert detail["candidate_revision"] == "demo-cb-failed"
            assert detail["service_url"] == URL and detail["service_access"] == "public"
            assert detail["digest"] == "sha256:" + "b" * 64
            assert [event["phase"] for event in detail["events"]][-4:] == [
                "checking",
                "restoring",
                "restore_checking",
                "failed",
            ]
            assert detail["events"][-1]["message"] == "신규 배포 실패 · 이전 버전 복구 완료"
            assert "never-publish-this" not in json.dumps(detail)
            assert "never-publish-this" not in json.dumps(store.get(rid)["checkpoint"])
            assert (
                client.get("/api/v1/releases", headers=HEADERS).json()[0]["rollback_status"]
                == "SUCCESS"
            )
            tick(GcpWorker(store, runtime))
            assert calls == ["POST", "GET", "GET", "GET", "GET"]
    finally:
        runtime.close()


@pytest.mark.parametrize("status", list(PHASES))
def test_all_upstream_states_supported(store, settings, status):
    row = enqueue(store, settings)
    calls = []

    def upstream(request):
        calls.append(request.method)
        return (
            httpx.Response(202, json={"deployment_id": REMOTE})
            if request.method == "POST"
            else httpx.Response(200, json=result(status=status))
        )

    runtime = GcpRuntime(settings, transport=httpx.MockTransport(upstream))
    try:
        worker = GcpWorker(store, runtime)
        tick(worker)
        tick(worker)
        assert store.get(row["id"])["state"] == PHASES[status][0]
        assert calls == ["POST", "GET"]
    finally:
        runtime.close()


def test_crash_after_intent_without_receipt_never_posts(store, settings):
    row = enqueue(store, settings)
    with store.connect() as conn:
        store.defer(conn, row, seconds=0, checkpoint={"submission_intent": True})
    calls = []
    runtime = GcpRuntime(settings, transport=httpx.MockTransport(lambda r: calls.append(r.method)))
    try:
        tick(GcpWorker(store, runtime))
        assert store.get(row["id"])["state"] == "uncertain"
        assert not calls
    finally:
        runtime.close()


def test_history_imports_missed_stages_without_raw_text(store, settings):
    history = ["CLONING", "BUILDING", "PUSHING", "DEPLOYING", "VERIFYING", "PROMOTING", "CHECKING"]
    row = enqueue(store, settings)
    runtime = GcpRuntime(
        settings,
        transport=httpx.MockTransport(
            lambda r: (
                httpx.Response(202, json={"deployment_id": REMOTE})
                if r.method == "POST"
                else httpx.Response(
                    200,
                    json=result(
                        status="CHECKING",
                        history=[{"status": s, "message": "raw"} for s in history],
                    ),
                )
            )
        ),
    )
    try:
        worker = GcpWorker(store, runtime)
        tick(worker)
        tick(worker)
        assert [e["state"] for e in store.get(row["id"])["events"]][-7:] == [
            PHASES[s][0] for s in history
        ]
        assert "message" not in json.dumps(store.get(row["id"])["checkpoint"]["public"])
    finally:
        runtime.close()


def test_metadata_is_structural_allowlist_and_digest_is_derived(settings):
    data = result(
        revision="credential",
        active_revision="demo-safe?token=raw",
        source_commit="raw",
        project_id="private key",
        url="https://attacker.example/?token=raw",
        digest="raw",
        error={"private_key": "raw"},
        credentials="raw",
        cloud_changes=[{"body": "raw"}],
        history=[{"status": "BUILDING", "message": "raw"}],
    )
    safe = public_metadata(data, settings)
    assert safe == {
        "image_uri": IMAGE,
        "digest": "sha256:" + "b" * 64,
        "health": "OK",
        "history": [{"status": "BUILDING", "phase": "building"}],
    }


def test_repository_and_native_forms_use_distinct_inputs(settings):
    adapter = GcpAdapter(settings)
    env = adapter.public_environment()
    assert env["input_kind"] == "repository" and env["service_access"] == "public"
    assert env["request_template"]["source"]["kind"] == "repository"
    assert env["request_template"]["strategy"] == "blue_green"
    assert adapter.capabilities().configurable_fields == ["strategy"]
    assert "rollout" not in adapter.capabilities().configurable_fields
    with pytest.raises(ValueError):
        adapter.parse_request(env["request_template"] | {"rollout": {"promotion": "manual"}})
