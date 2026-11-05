from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import EndpointConnectionError
from pydantic import ValidationError

from controlplane.builds.models import BuildInput, BuildSettings
from controlplane.builds.provider import BuildRejected, CodeBuild
from controlplane.builds.store import BuildStore, approved_image
from controlplane.builds.worker import BuildWorker
from controlplane.core.errors import RequestConflict

REPO = "https://github.com/example/demo"
IMAGE = "docker.io/team/demo@sha256:" + "a" * 64
BUILT = "docker.io/team/demo@sha256:" + "b" * 64
COMMIT = "c" * 40


def request(**changes):
    payload = dict(
        repository=REPO,
        revision="main",
        context=".",
        dockerfile="Dockerfile",
        idempotency_key="build-request-0001",
        deployment={
            "application": "demo",
            "environment_id": "gcp-demo",
            "source": {"kind": "image", "image": IMAGE},
            "architecture": "X86_64",
        },
    )
    payload.update(changes)
    return BuildInput.model_validate(payload)


def build_api(settings, store, deployments, identity=None):
    from fastapi import FastAPI

    from controlplane.api.deps import get_builds, require_operator
    from controlplane.api.errors import register_exception_handlers
    from controlplane.api.routes import builds
    from controlplane.services.builds import BuildService

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(builds.router)
    app.dependency_overrides[get_builds] = lambda: BuildService(settings, store, deployments)
    app.dependency_overrides[require_operator] = lambda: identity
    return app


def test_example_database_requirement_blocks_build_before_queueing(store):
    from fastapi.testclient import TestClient

    settings = BuildSettings(
        project="oneport-source-build",
        image_repository="docker.io/team/demo",
        repositories=[REPO],
        required_databases={REPO: "postgresql"},
    )
    BuildStore(store.dsn).migrate()
    deployments = Mock()
    app = build_api(settings, store, deployments)
    with TestClient(app) as client:
        assert client.get("/api/v1/builds/config").json()["required_databases"] == {
            REPO: "postgresql"
        }
        payload = request().model_dump(mode="json")
        response = client.post("/api/v1/builds", json=payload)
        assert response.status_code == 422 and "PostgreSQL" in response.json()["detail"]
        deployments.plan.assert_not_called()
        assert BuildStore(store.dsn).list() == []
        payload["deployment"]["resources"] = {
            "database": "postgresql",
            "database_mode": "container",
        }
        assert client.post("/api/v1/builds", json=payload).status_code == 202
        assert deployments.plan.call_count == 1


def test_general_repository_has_no_implicit_database_requirement():
    settings = BuildSettings(
        project="oneport-source-build", image_repository="docker.io/team/demo", repositories=[REPO]
    )
    assert settings.required_databases == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "https://evil.com/example/demo"),
        ("repository", "https://token@github.com/example/demo"),
        ("repository", "https://github.com/example/demo?token=x"),
        ("revision", "--upload-pack=evil"),
        ("context", "../outside"),
        ("dockerfile", "/etc/passwd"),
    ],
)
def test_build_input_rejects_untrusted_locations(field, value):
    with pytest.raises(ValidationError):
        request(**{field: value})


def test_build_journal_deduplicates_and_limits_queue(store):
    journal = BuildStore(store.dsn)
    journal.migrate()
    row = journal.enqueue(request())
    assert journal.enqueue(request())["id"] == row["id"]
    with pytest.raises(RequestConflict):
        journal.enqueue(request(revision="other"))
    with pytest.raises(RequestConflict):
        journal.enqueue(request(idempotency_key="another-build-0001"), max_pending=1)


def test_unlimited_builds_exceed_old_daily_and_queue_caps_but_keep_environment_lock(store):
    journal = BuildStore(store.dsn)
    journal.migrate()
    for index in range(35):
        req = request(idempotency_key=f"unlimited-build-{index:04}")
        req.deployment.environment_id = f"environment-{index}"
        journal.enqueue(req, max_pending=None, max_daily=None)
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM source_builds").fetchone()["n"] == 35
    req = request(idempotency_key="duplicate-environment-build")
    req.deployment.environment_id = "environment-0"
    with pytest.raises(RequestConflict, match="이 환경의 빌드"):
        journal.enqueue(req)
    req.deployment.environment_id = "environment-next"
    with pytest.raises(RequestConflict, match="24시간"):
        journal.enqueue(req, max_daily=30)
    with pytest.raises(RequestConflict, match="대기열"):
        journal.enqueue(req, max_pending=3)


def test_build_worker_starts_other_environments_while_first_build_is_running(store):
    journal = BuildStore(store.dsn)
    journal.migrate()
    first = journal.enqueue(request())
    provider = Mock()
    provider.resolve.return_value = COMMIT
    provider.start.side_effect = lambda row: "oneport-source-build:" + row["id"]
    provider.observe.return_value = {"buildStatus": "IN_PROGRESS"}
    worker = BuildWorker(journal, provider, Mock())
    for _ in range(3):
        worker.tick()
    req = request(idempotency_key="parallel-environment-build")
    req.deployment.environment_id = "second-environment"
    second = journal.enqueue(req)
    for _ in range(3):
        worker.tick()
    assert journal.get(first["id"])["state"] == "building"
    assert journal.get(second["id"])["state"] == "building"
    assert provider.start.call_count == 2
    worker.tick()
    assert provider.start.call_count == 2


def test_build_limit_settings_allow_none_and_opt_in_positive_caps():
    from controlplane.demo_deployments.models import RunSettings

    settings = BuildSettings(project="oneport-source-build", image_repository="docker.io/team/demo")
    assert settings.max_daily is None and settings.max_pending is None
    assert RunSettings().max_pending is None
    assert RunSettings(max_pending=None).max_pending is None
    for key in ("max_daily", "max_pending"):
        assert BuildSettings(**(settings.model_dump() | {key: 100})).model_dump()[key] == 100
        with pytest.raises(ValidationError):
            BuildSettings(**(settings.model_dump() | {key: 0}))


def test_restart_after_build_and_submission_reuses_durable_identity(store, monkeypatch):
    journal = BuildStore(store.dsn)
    journal.migrate()
    row = journal.enqueue(request())
    provider = Mock()
    provider.resolve.return_value = COMMIT
    provider.start.return_value = "oneport-source-build:abc"
    provider.observe.return_value = {"buildStatus": "SUCCEEDED"}
    provider.result_image.return_value = BUILT
    deployments = Mock()
    deployments.submit.return_value = SimpleNamespace(id="release-1")
    monkeypatch.setattr("controlplane.builds.worker.verify_public_image", lambda *args: None)
    for _ in range(4):
        BuildWorker(journal, provider, deployments).tick()
    assert journal.get(row["id"])["state"] == "deploying"
    assert approved_image(store.dsn, "demo", "gcp-demo", BUILT)
    assert not approved_image(store.dsn, "other", "gcp-demo", BUILT)
    assert not approved_image(store.dsn, "demo", "other-env", BUILT)
    BuildWorker(journal, provider, deployments).tick()
    done = journal.get(row["id"])
    assert done["release_id"] == "release-1" and done["state"] == "succeeded"
    # Operator/demo builds carry no requester (login mode passes the team's user).
    spec, key, requester = deployments.submit.call_args.args
    assert requester is None
    assert spec.source.image == BUILT and key == "source-build-" + row["id"]
    assert provider.start.call_count == 1


def test_submission_response_loss_does_not_replay_after_idempotency_window(store):
    journal = BuildStore(store.dsn)
    journal.migrate()
    row = journal.enqueue(request())
    clock = [1000]
    provider = Mock()
    provider.resolve.return_value = COMMIT
    provider.start.side_effect = EndpointConnectionError(endpoint_url="https://codebuild.example")
    worker = BuildWorker(journal, provider, Mock(), clock=lambda: clock[0])
    worker.tick()
    worker.tick()
    worker.tick()
    assert journal.get(row["id"])["state"] == "submitting"
    clock[0] += 241
    worker.tick()
    assert journal.get(row["id"])["state"] == "uncertain" and provider.start.call_count == 1


def test_codebuild_uses_owned_project_pinned_source_and_verifies_provenance():
    settings = BuildSettings(
        project="oneport-source-build", image_repository="docker.io/team/demo", repositories=[REPO]
    )
    client = Mock()
    p = CodeBuild(settings, client)
    client.start_build.return_value = {"build": {"id": "oneport-source-build:abc"}}
    row = {
        "id": "d" * 24,
        "request": request().model_dump(mode="json"),
        "checkpoint": {"commit": COMMIT},
    }
    p.start(row)
    args = client.start_build.call_args.kwargs
    assert args["sourceVersion"] == COMMIT and args["idempotencyToken"] == row["id"]
    assert "buildspecOverride" not in args
    assert args["timeoutInMinutesOverride"] == 15
    with pytest.raises(BuildRejected):
        p.result_image(row, {"resolvedSourceVersion": "e" * 40})
    with pytest.raises(BuildRejected):
        p.result_image(
            row,
            {
                "resolvedSourceVersion": COMMIT,
                "exportedEnvironmentVariables": [
                    {"name": "ONEPORT_IMAGE_DIGEST", "value": "tag-latest"}
                ],
            },
        )


def test_build_only_publishes_verified_image_without_deployment(store, monkeypatch):
    journal = BuildStore(store.dsn)
    journal.migrate()
    row = journal.enqueue(request(deploy_after_build=False))
    provider = Mock()
    provider.resolve.return_value = COMMIT
    provider.start.return_value = "oneport-source-build:abc"
    provider.observe.return_value = {"buildStatus": "SUCCEEDED"}
    provider.result_image.return_value = BUILT
    deployments = Mock()
    monkeypatch.setattr("controlplane.builds.worker.verify_public_image", lambda *args: None)
    for _ in range(4):
        BuildWorker(journal, provider, deployments).tick()
    result = journal.get(row["id"])
    assert result["state"] == "succeeded" and result["image"] == BUILT
    assert result["release_id"] is None and not deployments.submit.called


def test_reconcile_only_observes_existing_build_after_read_failure(store, monkeypatch):
    journal = BuildStore(store.dsn)
    journal.migrate()
    row = journal.enqueue(request(deploy_after_build=False))
    with journal.connect() as conn:
        journal.update(
            conn,
            row,
            "uncertain",
            "read failed",
            checkpoint={"build_id": "oneport-source-build:abc", "commit": COMMIT},
            error_code="build_provider_unavailable",
        )
    assert journal.reconcile(row["id"])["state"] == "building"
    provider = Mock()
    provider.observe.return_value = {"buildStatus": "SUCCEEDED"}
    provider.result_image.return_value = BUILT
    monkeypatch.setattr("controlplane.builds.worker.verify_public_image", lambda *args: None)
    BuildWorker(journal, provider, Mock()).tick()
    provider.start.assert_not_called()
    assert journal.get(row["id"])["state"] == "succeeded"
    with pytest.raises(RequestConflict):
        journal.reconcile(row["id"])


def test_reconcile_rejects_missing_execution_handle(store):
    journal = BuildStore(store.dsn)
    journal.migrate()
    row = journal.enqueue(request())
    with journal.connect() as conn:
        journal.update(
            conn, row, "uncertain", "submit failed", error_code="build_provider_unavailable"
        )
    with pytest.raises(RequestConflict):
        journal.reconcile(row["id"])


@pytest.mark.parametrize("suffix", ["", "/", ".git", ".git/"])
def test_build_input_normalizes_public_github_clone_urls(suffix):
    assert request(repository=REPO + suffix).repository == REPO


@pytest.mark.parametrize(
    "repository",
    [
        "http://github.com/example/demo",
        "https://github.com.evil.test/example/demo",
        "https://github.com:443/example/demo",
        "https://github.com/example/demo#main",
        "https://github.com/example/demo/tree/main",
        "https://github.com/../demo",
        "https://github.com/example/..",
        "https://github.com/example/..git",
        "https://github.com/example/.git",
        "https://github.com/example/%2e%2e",
        "https://169.254.169.254/latest/meta-data",
        "git@github.com:example/demo.git",
    ],
)
def test_public_sources_still_reject_non_repository_urls_before_network(repository, monkeypatch):
    with pytest.raises(ValidationError):
        request(repository=repository)
    http = Mock()
    monkeypatch.setattr("controlplane.builds.provider.httpx.Client", http)
    client = Mock()
    provider = CodeBuild(build_settings(), client)
    with pytest.raises(BuildRejected, match="invalid_repository_url"):
        provider.resolve(repository, "main")
    row = {"id": "d" * 24, "request": {"repository": repository}, "checkpoint": {"commit": COMMIT}}
    with pytest.raises(BuildRejected, match="invalid_repository_url"):
        provider.start(row)
    http.assert_not_called()
    client.start_build.assert_not_called()


def build_settings(**changes):
    return BuildSettings(
        project="oneport-source-build", image_repository="docker.io/team/demo", **changes
    )


@pytest.mark.parametrize("suggestions", [[], ["https://github.com/old/example"]])
def test_unlisted_public_repository_resolves_and_starts_pinned_build(suggestions, monkeypatch):
    import httpx

    seen = []

    def github(req):
        seen.append(req)
        assert str(req.url) == "https://api.github.com/repos/example/demo/commits/1.2.1"
        assert "authorization" not in req.headers
        return httpx.Response(200, json={"sha": COMMIT})

    real_client = httpx.Client
    client_options = []

    def http_client(**options):
        client_options.append(options)
        return real_client(transport=httpx.MockTransport(github), **options)

    monkeypatch.setattr("controlplane.builds.provider.httpx.Client", http_client)
    aws = Mock()
    aws.start_build.return_value = {"build": {"id": "oneport-source-build:abc"}}
    provider = CodeBuild(build_settings(repositories=suggestions), aws)
    resolved = provider.resolve(REPO + ".git/", "1.2.1")
    assert resolved == COMMIT and len(seen) == 1
    assert client_options[0]["follow_redirects"] is False
    assert client_options[0]["trust_env"] is False
    row = {
        "id": "d" * 24,
        "request": request(repository=REPO + ".git", revision="1.2.1").model_dump(mode="json"),
        "checkpoint": {"commit": resolved},
    }
    provider.start(row)
    args = aws.start_build.call_args.kwargs
    assert args["sourceLocationOverride"] == REPO
    assert args["sourceVersion"] == COMMIT
    assert "buildspecOverride" not in args


@pytest.mark.parametrize("status", [301, 302, 403, 404, 429])
def test_inaccessible_or_redirected_public_source_fails_before_build(status, monkeypatch):
    import httpx

    calls = []

    def github(req):
        calls.append(req)
        return httpx.Response(status, headers={"Location": "https://outside.test/private"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "controlplane.builds.provider.httpx.Client",
        lambda **options: real_client(transport=httpx.MockTransport(github), **options),
    )
    aws = Mock()
    provider = CodeBuild(build_settings(), aws)
    with pytest.raises(BuildRejected, match="revision_not_found"):
        provider.resolve(REPO, "main")
    assert len(calls) == 1
    aws.start_build.assert_not_called()


def test_worker_rejects_unpinned_commit_before_starting_build():
    aws = Mock()
    provider = CodeBuild(build_settings(), aws)
    row = {
        "id": "d" * 24,
        "request": request().model_dump(mode="json"),
        "checkpoint": {"commit": "main"},
    }
    with pytest.raises(BuildRejected, match="invalid_source_commit"):
        provider.start(row)
    aws.start_build.assert_not_called()


def test_build_api_accepts_unlisted_repository_and_preserves_ownership(store):
    from fastapi.testclient import TestClient

    from controlplane.schemas.accounts import Identity

    identity = Identity(email="owner@example.com", team="alpha", role="owner")
    deployments = Mock()
    deployments.plan.return_value = SimpleNamespace(fingerprint="a" * 64)
    deployments.accounts.owners.return_value = {}
    journal = BuildStore(store.dsn)
    journal.migrate()
    app = build_api(
        build_settings(repositories=["https://github.com/old/example"]),
        store,
        deployments,
        identity,
    )
    with TestClient(app) as http:
        payload = request().model_dump(mode="json")
        response = http.post("/api/v1/builds", json=payload)
        assert response.status_code == 202, response.text
        row = journal.get(response.json()["id"])
        assert row["request"]["repository"] == REPO and row["team"] == identity.team
        deployments.accounts.owners.return_value = {"gcp-demo": "another-team"}
        payload["idempotency_key"] = "another-team-build-01"
        assert http.post("/api/v1/builds", json=payload).status_code == 409
        assert len(journal.list()) == 1


def test_build_history_pages_are_environment_and_team_scoped(store):
    from fastapi.testclient import TestClient

    from controlplane.schemas.accounts import Identity

    journal = BuildStore(store.dsn)
    journal.migrate()
    rows = []
    for index, (environment, team) in enumerate(
        [("gcp-demo", "red"), ("other", "red"), ("gcp-demo", "blue"), ("gcp-demo", "red")]
    ):
        deployment = request().deployment.model_dump(mode="json") | {"environment_id": environment}
        row = journal.enqueue(
            request(idempotency_key=f"history-build-key-{index}", deployment=deployment), team=team
        )
        with journal.connect() as conn:
            journal.update(conn, row, "failed", "Build failed")
        rows.append(row)
    identity = Identity(email="owner@example.com", team="red", role="owner")
    app = build_api(build_settings(repositories=[REPO]), store, Mock(), identity)
    with TestClient(app) as api:
        path = "/api/v1/builds?environment_id=gcp-demo&limit=1"
        assert [r["id"] for r in api.get(path).json()] == [rows[3]["id"]]
        assert [r["id"] for r in api.get(path + "&offset=1").json()] == [rows[0]["id"]]
        assert api.get(path + "&offset=2").json() == []
        assert api.get("/api/v1/builds?limit=101").status_code == 422
        assert api.get("/api/v1/builds?offset=-1").status_code == 422
