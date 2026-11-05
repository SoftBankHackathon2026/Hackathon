"""Real PostgreSQL journals; cloud/build calls are isolated at the provider boundary."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from test_helm_worker import settings as settings

from controlplane.builds.models import BuildSettings
from controlplane.builds.store import BuildStore
from controlplane.builds.worker import BuildWorker
from controlplane.core.errors import RequestConflict
from controlplane.demo_deployments.artifacts import approved_demo_image
from controlplane.demo_deployments.models import PreviewInput, RetryRun, RunSettings, StartRun
from controlplane.demo_deployments.service import RunService
from controlplane.demo_deployments.store import RunNotFound, RunStore
from controlplane.demo_deployments.worker import RunWorker
from controlplane.domain.models import DeploymentRequest, Runtime
from controlplane.main import create_app
from controlplane.providers.capabilities import provider_capabilities
from controlplane.providers.durable import DurableDeploymentProvider
from controlplane.providers.interface import DeploymentSpec
from controlplane.providers.onprem.helm.adapter import HelmAdapter
from controlplane.providers.registry import ProviderRegistry
from controlplane.schemas.accounts import Identity
from controlplane.services.deployments import DeploymentService

REPO = "https://github.com/example/demo"
IMAGE = "docker.io/example/demo@sha256:" + "a" * 64
BUILT = "docker.io/example/demo@sha256:" + "b" * 64
COMMIT = "c" * 40
TEAM = Identity(email="owner@example.com", team="alpha", role="owner")


class Adapter(DurableDeploymentProvider):
    def __init__(self, store, runtime, name):
        self.application, self.environment_id, self.runtime = "demo", name, runtime
        self.settings = SimpleNamespace(
            database_url=SecretStr(store.dsn),
            data_provisioning=SimpleNamespace(),
            cache_provisioning=SimpleNamespace(),
        )
        self.store, self.version = store, "binding-v1"

    def capabilities(self):
        if self.runtime == Runtime.GCP_CLOUD_RUN:
            from controlplane.providers.gcp.adapter import GcpAdapter

            return GcpAdapter.capabilities(self)
        return provider_capabilities(self.runtime)

    def fingerprint(self):
        return self.version

    def parse_request(self, payload):
        request = DeploymentRequest.model_validate(payload)
        if request.source.image != IMAGE and not approved_demo_image(
            self.store.dsn, self.application, self.environment_id, request.source.image
        ):
            raise ValueError("Unapproved artifact")
        return request

    def plan_steps(self, request):
        return []

    def public_environment(self):
        return {
            "request_template": {
                "source": {"kind": "image", "image": IMAGE},
                "health_path": "/health",
            }
        }


@pytest.fixture
def setup(store):
    registry = ProviderRegistry()
    for runtime, name in [
        (Runtime.AWS_ECS, "aws"),
        (Runtime.GCP_CLOUD_RUN, "gcp"),
        (Runtime.ONPREM_HELM, "helm"),
    ]:
        registry.register(Adapter(store, runtime, name))
    registry.enable_catalog(store)
    registry.initialize()
    app_id = registry.catalog.list_applications()[0]["id"]
    with store.connect() as conn:
        conn.execute("UPDATE applications SET team=%s WHERE id=%s", (TEAM.team, app_id))
    settings = RunSettings(
        targets={
            "aws_ecs": "aws",
            "gcp_cloud_run": "gcp",
            "onprem_helm": "helm",
        }
    )
    builds = BuildSettings(
        project="oneport-build", image_repository="docker.io/example/demo", repositories=[REPO]
    )
    BuildStore(store.dsn).migrate()
    RunStore(store.dsn).migrate()
    for environment_id in ("aws", "gcp", "helm"):
        from controlplane.repositories.accounts import AccountStore

        AccountStore(store).claim(environment_id, TEAM)
    resolver = Mock()
    resolver.resolve.return_value = COMMIT
    service = RunService(settings, builds, DeploymentService(registry, store), resolver)
    return service, app_id


def preview(setup, **kwargs):
    service, app_id = setup
    return service.preview(PreviewInput(repository=REPO, **kwargs), TEAM)


@pytest.fixture
def helm_setup(setup, settings, monkeypatch):
    service, _ = setup
    settings.database_url = SecretStr(service.store.dsn)
    settings.environment.application = "demo"
    settings.environment.environment_id = "helm"
    settings.allowed_images = [IMAGE]
    settings.demo_database = False
    settings.catalog_dynamic = True
    adapter = HelmAdapter(settings, store=service.deployments.store)
    monkeypatch.setitem(service.deployments.registry._bindings, "helm", adapter)
    return setup


@pytest.mark.parametrize("catalog_app", [False, True])
def test_multi_deploy_defaults_new_helm_and_gcp_to_postgresql_and_redis(helm_setup, catalog_app):
    service, app_id = helm_setup
    p = preview(helm_setup, **({"application_id": app_id} if catalog_app else {}))
    assert p["ready"], p
    specs = {t["runtime"]: t["spec"] for t in p["targets"]}
    assert specs["onprem_helm"]["resources"] == {
        "database": "postgresql",
        "database_mode": "container",
        "redis": True,
    }
    assert not specs["aws_ecs"].get("resources")
    assert specs["gcp_cloud_run"]["resources"] == {
        "database": "postgresql",
        "database_mode": "dedicated",
        "redis": True,
    }
    # A normal individual deployment still starts without a database.
    original = service.deployments.registry.get("helm").public_environment()
    assert original["request_template"]["resources"]["database"] == "none"
    assert not service.deployments.store.list()
    assert not BuildStore(service.store.dsn).list()


@pytest.mark.parametrize("history", [["failed"], ["succeeded"], ["succeeded", "failed"]])
def test_multi_deploy_preserves_successful_helm_but_corrects_failed_first_deploy(
    helm_setup, history
):
    service, _ = helm_setup
    adapter = service.deployments.registry.get("helm")
    for index, state in enumerate(history):
        spec = DeploymentSpec.model_validate(adapter.public_environment()["request_template"])
        release = service.deployments.submit(spec, f"prior-helm-release-{index}", TEAM)
        with service.store.connect() as conn:
            conn.execute("UPDATE releases SET state=%s WHERE id=%s", (state, release.id))
    p = preview(helm_setup)
    assert p["ready"], p
    helm = next(t for t in p["targets"] if t["runtime"] == "onprem_helm")
    assert helm["spec"]["resources"]["database"] == (
        "none" if "succeeded" in history else "postgresql"
    )
    assert helm["spec"]["resources"]["redis"] == ("succeeded" not in history)


@pytest.mark.parametrize("postgresql", [False, True])
def test_multi_deploy_keeps_existing_helm_data_configuration(helm_setup, monkeypatch, postgresql):
    service, _ = helm_setup
    adapter = service.deployments.registry.get("helm")
    monkeypatch.setattr(
        adapter,
        "saved_data",
        lambda: {"postgresql": {"enabled": postgresql}, "redis": {"enabled": True}},
    )
    p = preview(helm_setup)
    assert p["ready"], p
    helm = next(t for t in p["targets"] if t["runtime"] == "onprem_helm")
    assert helm["spec"]["resources"] == {
        "database": "postgresql" if postgresql else "none",
        "database_mode": "container",
        "redis": True,
    }


def test_multi_deploy_submits_postgresql_to_real_helm_adapter(helm_setup, monkeypatch):
    service, _ = helm_setup
    run_id, _ = built(helm_setup, monkeypatch)
    RunWorker(service).tick()
    target = next(t for t in service.store.get(run_id)["targets"] if t["runtime"] == "onprem_helm")
    release = service.deployments.store.get(target["release_id"])
    assert release["request"]["resources"]["database"] == "postgresql"
    assert release["request"]["resources"]["database_mode"] == "container"
    assert release["request"]["resources"]["redis"] is True


def start(setup, key="start-run-00000001"):
    service, _ = setup
    p = preview(setup)
    assert p["ready"], p
    return service.start(StartRun(preview_id=p["id"], idempotency_key=key), TEAM)


def built(setup, monkeypatch):
    service, _ = setup
    row = start(setup)
    RunWorker(service).tick()
    build = Mock()
    build.resolve.return_value = COMMIT
    build.start.return_value = "oneport-build:abc"
    build.observe.return_value = {"buildStatus": "SUCCEEDED"}
    build.result_image.return_value = BUILT
    monkeypatch.setattr("controlplane.builds.worker.verify_public_image", lambda *args: None)
    for _ in range(4):
        BuildWorker(BuildStore(service.store.dsn), build, service.deployments).tick()
    RunWorker(service).tick()
    assert service.store.get(row["id"])["state"] == "deploying"
    return row["id"], build


def finish(service, run_id, statuses):
    with service.store.connect() as conn:
        for runtime, state in statuses.items():
            target = next(
                t for t in service.store.get(run_id)["targets"] if t["runtime"] == runtime
            )
            conn.execute(
                "UPDATE releases SET state=%s, service_url=%s WHERE id=%s",
                (state, "https://" + runtime.replace("_", "-") + ".example", target["release_id"]),
            )
    RunWorker(service).tick()
    return service.store.get(run_id)


def test_preview_pins_default_branch_without_building_or_releasing(setup):
    service, app_id = setup
    p = preview(setup)
    service.resolver.resolve.assert_called_once_with(REPO, None)
    assert p["commit"] == COMMIT
    assert all(t["spec"]["health_path"] == "/health" for t in p["targets"])
    assert not service.deployments.store.list()
    assert not BuildStore(service.store.dsn).list()


def test_one_destination_can_change_its_defaults_for_this_run(setup):
    p = preview(setup, port=9000, overrides={"gcp_cloud_run": {"port": 9100, "health_path": "/up"}})
    assert p["ready"], p
    specs = {t["runtime"]: t["spec"] for t in p["targets"]}
    assert (specs["gcp_cloud_run"]["port"], specs["gcp_cloud_run"]["health_path"]) == (9100, "/up")
    assert (specs["aws_ecs"]["port"], specs["aws_ecs"]["health_path"]) == (9000, "/health")
    # The provider still decides: a value it does not allow blocks only that destination.
    rejected = preview(setup, overrides={"aws_ecs": {"cpu": "64"}})
    blocked = {t["runtime"]: t["blockers"] for t in rejected["targets"]}
    assert blocked["aws_ecs"] == ["deployment_defaults_rejected"] and not blocked["onprem_helm"]
    with pytest.raises(ValueError):
        PreviewInput(repository=REPO, overrides={"aws_ecs": {"image": "evil"}})


def test_catalog_app_deploys_to_its_own_environment_on_each_provider(setup):
    service, app_id = setup
    # No fixed demo destinations: a run names the catalog app it deploys.
    service.settings = RunSettings()
    p = preview(setup, application_id=app_id)
    assert p["ready"], p
    assert {t["runtime"]: t["environment_id"] for t in p["targets"]} == {
        "aws_ecs": "aws",
        "gcp_cloud_run": "gcp",
        "onprem_helm": "helm",
    }
    run = service.start(StartRun(preview_id=p["id"], idempotency_key="app-run-000000001"), TEAM)
    assert {t["environment_id"] for t in run["targets"]} == {"aws", "gcp", "helm"}
    # Without an app, or with another team's app, nothing is deployable.
    assert not preview(setup)["ready"]
    other = service.preview(
        PreviewInput(repository=REPO, application_id=app_id),
        Identity(email="b@ex.com", team="beta"),
    )
    assert all(t["blockers"] == ["target_not_configured"] for t in other["targets"])


def test_url_only_resolves_explicit_demo_targets_without_catalog_creation(setup):
    service, _ = setup
    p = service.preview(PreviewInput(repository=REPO), TEAM)
    assert p["ready"]
    assert {t["environment_id"] for t in p["targets"]} == {"aws", "gcp", "helm"}
    assert not BuildStore(service.store.dsn).list()
    assert len(service.deployments.registry.catalog.list_applications()) == 1


def test_intake_idempotent_after_expiry_and_conflicting_keys_rejected(setup):
    service, _ = setup
    p = preview(setup)
    request = StartRun(preview_id=p["id"], idempotency_key="start-run-00000001")
    first = service.start(request, TEAM)
    with service.store.connect() as conn:
        conn.execute("UPDATE demo_deployment_previews SET expires_at=now()-interval '1 second'")
    assert service.start(request, TEAM)["id"] == first["id"]
    with pytest.raises(RequestConflict):
        service.start(request.model_copy(update={"idempotency_key": "different-run-key"}), TEAM)
    with pytest.raises(RequestConflict):
        start(setup, "another-start-key1")


def test_owner_and_binding_drift_checked_before_build_cost(setup):
    service, app_id = setup
    blocked = service.preview(
        PreviewInput(repository=REPO), Identity(email="b@ex.com", team="beta")
    )
    assert not blocked["ready"] and all(t["spec"] is None for t in blocked["targets"])
    p = preview(setup)
    service.deployments.registry.get("gcp").version = "binding-v2"
    with pytest.raises(RequestConflict):
        service.start(StartRun(preview_id=p["id"], idempotency_key="changed-run-000001"), TEAM)
    assert not BuildStore(service.store.dsn).list()


def test_single_build_shared_by_three_releases_and_waits_for_actual_success(setup, monkeypatch):
    service, _ = setup
    run_id, provider = built(setup, monkeypatch)
    RunWorker(service).tick()
    running = service.store.get(run_id)
    assert (
        running["state"] == "deploying" and len({t["release_id"] for t in running["targets"]}) == 3
    )
    releases = service.deployments.store.list()
    assert len(releases) == 3
    assert {r["request"]["source"]["image"] for r in releases} == {BUILT}
    provider.start.assert_called_once()
    provider.resolve.assert_called_once_with(REPO, COMMIT)
    done = finish(service, run_id, {r: "succeeded" for r in service.settings.targets})
    assert done["state"] == "succeeded"
    assert all(t["service_url"] and len(t["attempts"]) == 1 for t in done["targets"])


def test_failed_target_retry_keeps_successes_digest_and_history(setup, monkeypatch):
    service, _ = setup
    run_id, provider = built(setup, monkeypatch)
    RunWorker(service).tick()
    partial = finish(
        service,
        run_id,
        {"aws_ecs": "succeeded", "gcp_cloud_run": "failed", "onprem_helm": "succeeded"},
    )
    assert partial["state"] == "partial"
    request = RetryRun(runtimes=["gcp_cloud_run"], idempotency_key="retry-gcp-0000001")
    service.retry(run_id, request, TEAM)
    service.retry(run_id, request, TEAM)
    RunWorker(service).tick()
    after = service.store.get(run_id)
    for before, target in zip(partial["targets"], after["targets"]):
        if target["runtime"] == "gcp_cloud_run":
            assert target["attempt"] == 2 and len(target["attempts"]) == 2
            assert target["release_id"] != before["release_id"]
        else:
            assert target == before
    assert len(BuildStore(service.store.dsn).list()) == 1
    assert after["image"] == BUILT and provider.start.call_count == 1
    assert finish(service, run_id, {"gcp_cloud_run": "succeeded"})["state"] == "succeeded"


def test_lost_submission_response_and_restart_adopt_child_instead_of_duplicate(setup, monkeypatch):
    service, _ = setup
    run_id, _ = built(setup, monkeypatch)
    original = service.deployments.submit

    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated response loss")

    monkeypatch.setattr(service.deployments, "submit", lost)
    RunWorker(service).tick()
    assert len(service.deployments.store.list()) == 3
    monkeypatch.setattr(
        service.deployments, "submit", Mock(side_effect=AssertionError("must not replay"))
    )
    RunWorker(service).tick()
    assert len(service.deployments.store.list()) == 3
    assert all(t["release_id"] for t in service.store.get(run_id)["targets"])


def test_uncertain_target_cannot_be_retried_and_does_not_block_others(setup, monkeypatch):
    service, _ = setup
    run_id, _ = built(setup, monkeypatch)
    RunWorker(service).tick()
    row = finish(
        service,
        run_id,
        {"aws_ecs": "uncertain", "gcp_cloud_run": "succeeded", "onprem_helm": "succeeded"},
    )
    assert row["state"] == "uncertain"
    with pytest.raises(RequestConflict):
        service.retry(
            run_id, RetryRun(runtimes=["aws_ecs"], idempotency_key="retry-unknown-001"), TEAM
        )
    assert finish(service, run_id, {"aws_ecs": "succeeded"})["state"] == "succeeded"


def test_artifact_permissions_follow_app_and_team_not_registry_image_name(setup, monkeypatch):
    service, app_id = setup
    built(setup, monkeypatch)
    assert approved_demo_image(service.store.dsn, "demo", "gcp", BUILT)
    assert not approved_demo_image(service.store.dsn, "demo", "other", BUILT)
    assert not approved_demo_image(service.store.dsn, "other", "gcp", BUILT)
    with service.store.connect() as conn:
        conn.execute("UPDATE environment_teams SET team='beta' WHERE environment_id=%s", ("gcp",))
    assert not approved_demo_image(service.store.dsn, "demo", "gcp", BUILT)


def test_failed_build_never_deploys_and_cross_team_run_not_visible(setup):
    service, _ = setup
    run = start(setup)
    RunWorker(service).tick()
    with service.store.connect() as conn:
        conn.execute("UPDATE source_builds SET state='failed',error_code='codebuild_failed'")
    RunWorker(service).tick()
    assert service.store.get(run["id"])["state"] == "failed"
    assert not service.deployments.store.list()
    with pytest.raises(RunNotFound):
        service.store.get(run["id"], Identity(email="b@ex.com", team="beta"))


def test_api_disabled_by_default_and_auth_enforced(setup, monkeypatch):
    service, _ = setup
    monkeypatch.delenv("ONEPORT_DEMO_DEPLOY_CONFIG", raising=False)
    monkeypatch.delenv("ONEPORT_BUILD_CONFIG", raising=False)
    token = "a-test-token-with-more-than-32-characters"
    with TestClient(
        create_app(
            registry=service.deployments.registry,
            store=service.deployments.store,
            operator_token=token,
        )
    ) as client:
        assert (
            client.post("/api/v1/demo/deployment-previews", json={"repository": REPO}).status_code
            == 401
        )
        assert (
            client.post(
                "/api/v1/demo/deployment-previews",
                headers={"Authorization": "Bearer " + token},
                json={"repository": REPO},
            ).status_code
            == 503
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"repository": "https://token@github.com/a/b"},
        {"context": "../secret"},
        {"revision": "--evil"},
        {"port": 80},
    ],
)
def test_input_contract_rejects_invalid_locations_and_ports(changes):
    with pytest.raises(ValidationError):
        PreviewInput(**{"repository": REPO, **changes})


def test_single_call_endpoint_is_idempotent_without_another_github_request(
    setup, tmp_path, monkeypatch
):
    from controlplane.demo_deployments.source import DemoSource

    service, _ = setup
    demo_file, build_file = tmp_path / "demo.json", tmp_path / "build.json"
    demo_file.write_text(service.settings.model_dump_json())
    build_file.write_text(service.build_settings.model_dump_json())
    monkeypatch.setenv("ONEPORT_DEMO_DEPLOY_CONFIG", str(demo_file))
    monkeypatch.setenv("ONEPORT_BUILD_CONFIG", str(build_file))
    resolve = Mock(return_value=COMMIT)
    monkeypatch.setattr(DemoSource, "resolve", resolve)
    token = "a-test-token-with-more-than-32-characters"
    with TestClient(
        create_app(
            registry=service.deployments.registry,
            store=service.deployments.store,
            operator_token=token,
        )
    ) as client:
        headers = {"Authorization": "Bearer " + token}
        payload = {"repository": REPO, "idempotency_key": "one-click-demo-00001"}
        first = client.post("/api/v1/demo/deployments", headers=headers, json=payload)
        assert first.status_code == 202, first.text
        second = client.post("/api/v1/demo/deployments", headers=headers, json=payload)
        assert second.status_code == 202 and second.json()["id"] == first.json()["id"]
        resolve.assert_called_once()
        conflict = client.post(
            "/api/v1/demo/deployments", headers=headers, json={**payload, "revision": "other"}
        )
        assert conflict.status_code == 409
        result = client.get("/api/v1/demo/deployment-runs/" + first.json()["id"], headers=headers)
        assert result.status_code == 200 and len(result.json()["targets"]) == 3
        assert "binding_fingerprint" not in result.text and "snapshot" not in result.text


def test_default_branch_resolution_reuses_existing_codebuild_resolver(setup, monkeypatch):
    from controlplane.builds.provider import CodeBuild
    from controlplane.demo_deployments.source import DemoSource

    service, _ = setup
    response = Mock(status_code=200)
    response.json.return_value = {"default_branch": "develop"}
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock()
    client.get.return_value = response
    monkeypatch.setattr(
        "controlplane.demo_deployments.source.httpx.Client", lambda **kwargs: client
    )
    resolve = Mock(return_value=COMMIT)
    monkeypatch.setattr(CodeBuild, "resolve", resolve)
    assert DemoSource(service.build_settings).resolve(REPO, None) == COMMIT
    resolve.assert_called_once_with(REPO, "develop")
    assert client.get.call_args.args[0] == "https://api.github.com/repos/example/demo"


def test_demo_does_not_replace_a_deployment_started_during_build(setup, monkeypatch):
    service, _ = setup
    run_id, _ = built(setup, monkeypatch)
    frozen = service.store.get(run_id)["snapshot"]["targets"][1]["spec"]
    from controlplane.providers.interface import DeploymentSpec

    manual = service.deployments.submit(
        DeploymentSpec.model_validate(frozen), "manual-deploy-000001", TEAM
    )
    with service.store.connect() as conn:
        conn.execute("UPDATE releases SET state='succeeded' WHERE id=%s", (manual.id,))
    RunWorker(service).tick()
    row = service.store.get(run_id)
    gcp = next(t for t in row["targets"] if t["runtime"] == "gcp_cloud_run")
    assert gcp["state"] == "failed" and gcp["release_id"] is None
    assert len(service.deployments.store.list()) == 3  # two demo releases plus the manual one


def test_missing_optional_demo_tables_does_not_approve_images(store):
    assert not approved_demo_image(store.dsn, "demo", "gcp", BUILT)


def test_unrelated_binding_cannot_reuse_verified_demo_artifact(setup, monkeypatch):
    service, _ = setup
    built(setup, monkeypatch)
    from controlplane.repositories.accounts import AccountStore

    AccountStore(service.deployments.store).claim("unrelated", TEAM)
    assert not approved_demo_image(service.store.dsn, "demo", "unrelated", BUILT)


def test_different_app_names_share_only_the_three_configured_bindings(setup, monkeypatch):
    service, _ = setup
    for env, adapter in service.deployments.registry._bindings.items():
        adapter.application = env + "-demo"
    run_id, _ = built(setup, monkeypatch)
    RunWorker(service).tick()
    row = service.store.get(run_id)
    assert {t["application"] for t in row["targets"]} == {"aws-demo", "gcp-demo", "helm-demo"}
    assert all(t["release_id"] for t in row["targets"])
    assert approved_demo_image(service.store.dsn, "gcp-demo", "gcp", BUILT)
    assert not approved_demo_image(service.store.dsn, "aws-demo", "gcp", BUILT)


def test_concurrent_single_call_requests_create_one_run(setup):
    from concurrent.futures import ThreadPoolExecutor

    from controlplane.demo_deployments.models import DemoDeployInput

    service, _ = setup
    request = DemoDeployInput(repository=REPO, idempotency_key="concurrent-click-01")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.launch(request, TEAM), range(2)))
    assert results[0]["id"] == results[1]["id"]
    with service.store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM demo_deployment_runs").fetchone()["n"] == 1
    assert not BuildStore(service.store.dsn).list()


def test_login_viewer_can_read_own_run_but_cannot_start_or_retry(setup, tmp_path, monkeypatch):
    from controlplane.demo_deployments.source import DemoSource

    service, _ = setup
    demo_file, build_file = tmp_path / "demo.json", tmp_path / "build.json"
    demo_file.write_text(service.settings.model_dump_json())
    build_file.write_text(service.build_settings.model_dump_json())
    monkeypatch.setenv("ONEPORT_DEMO_DEPLOY_CONFIG", str(demo_file))
    monkeypatch.setenv("ONEPORT_BUILD_CONFIG", str(build_file))
    resolve = Mock(return_value=COMMIT)
    monkeypatch.setattr(DemoSource, "resolve", resolve)
    with TestClient(
        create_app(
            registry=service.deployments.registry,
            store=service.deployments.store,
            auth_mode="login",
        )
    ) as client:
        client.headers["Origin"] = "http://127.0.0.1:4320"
        owner = client.post(
            "/api/v1/auth/signup",
            json={"email": TEAM.email, "password": "test-password-1234", "team": TEAM.team},
        )
        assert owner.status_code == 201, owner.text
        run = client.post(
            "/api/v1/demo/deployments",
            json={"repository": REPO, "idempotency_key": "owner-click-000001"},
        )
        assert run.status_code == 202, run.text
        viewer = client.post(
            "/api/v1/auth/signup",
            json={
                "email": "viewer@example.com",
                "password": "test-password-1234",
                "team": TEAM.team,
            },
        )
        assert viewer.status_code == 201 and viewer.json()["role"] == "viewer"
        assert client.get("/api/v1/demo/deployment-runs/" + run.json()["id"]).status_code == 200
        assert (
            client.post(
                "/api/v1/demo/deployments",
                json={"repository": REPO, "idempotency_key": "viewer-click-00001"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/demo/deployment-runs/" + run.json()["id"] + "/retry",
                json={"runtimes": ["aws_ecs"], "idempotency_key": "viewer-retry-00001"},
            ).status_code
            == 403
        )
        resolve.assert_called_once()


def test_operator_can_deploy_configured_team_owned_targets(setup, monkeypatch):
    service, _ = setup
    p = service.preview(PreviewInput(repository=REPO), None)
    assert p["ready"]
    run = service.start(StartRun(preview_id=p["id"], idempotency_key="operator-start-00001"), None)
    RunWorker(service).tick()
    build = Mock()
    build.resolve.return_value = COMMIT
    build.start.return_value = "oneport-build:abc"
    build.observe.return_value = {"buildStatus": "SUCCEEDED"}
    build.result_image.return_value = BUILT
    monkeypatch.setattr("controlplane.builds.worker.verify_public_image", lambda *args: None)
    for _ in range(4):
        BuildWorker(BuildStore(service.store.dsn), build, service.deployments).tick()
    RunWorker(service).tick()
    RunWorker(service).tick()
    assert all(t["release_id"] for t in service.store.get(run["id"])["targets"])


def test_retry_after_definite_pre_intake_failure_remains_retryable(setup, monkeypatch):
    service, _ = setup
    run_id, _ = built(setup, monkeypatch)
    RunWorker(service).tick()
    finish(
        service,
        run_id,
        {"aws_ecs": "succeeded", "gcp_cloud_run": "failed", "onprem_helm": "succeeded"},
    )
    service.retry(
        run_id, RetryRun(runtimes=["gcp_cloud_run"], idempotency_key="retry-intake-000001"), TEAM
    )
    original = service.deployments.submit
    monkeypatch.setattr(
        service.deployments,
        "submit",
        Mock(side_effect=RequestConflict("temporary configuration switch")),
    )
    RunWorker(service).tick()
    assert service.store.get(run_id)["state"] == "partial"
    monkeypatch.setattr(service.deployments, "submit", original)
    service.retry(
        run_id, RetryRun(runtimes=["gcp_cloud_run"], idempotency_key="retry-intake-000002"), TEAM
    )
    RunWorker(service).tick()
    gcp = next(t for t in service.store.get(run_id)["targets"] if t["runtime"] == "gcp_cloud_run")
    assert gcp["release_id"] and gcp["attempt"] == 3


def test_restart_does_not_adopt_build_with_wrong_commit(setup):
    from psycopg.types.json import Jsonb

    service, _ = setup
    run = start(setup)
    RunWorker(service).tick()
    with service.store.connect() as conn:
        conn.execute(
            """UPDATE source_builds SET state='succeeded', image=%s, checkpoint=%s,
            request=jsonb_set(request,'{revision}',to_jsonb(%s::text))""",
            (BUILT, Jsonb({"commit": "d" * 40}), "d" * 40),
        )
    RunWorker(service).tick()
    row = service.store.get(run["id"])
    assert row["state"] == "uncertain" and row["image"] is None
    assert not service.deployments.store.list()


def test_restart_rejects_child_with_same_key_but_different_request(setup, monkeypatch):
    from controlplane.providers.interface import DeploymentSpec

    service, _ = setup
    run_id, _ = built(setup, monkeypatch)
    target = next(
        t
        for t in service.store.get(run_id)["snapshot"]["targets"]
        if t["runtime"] == "gcp_cloud_run"
    )
    payload = {
        **target["spec"],
        "source": {"kind": "image", "image": BUILT},
        "env": {"UNEXPECTED": "value"},
    }
    key = f"run-{run_id}-gcp_cloud_run-1"
    service.deployments.submit(DeploymentSpec.model_validate(payload), key, TEAM)
    RunWorker(service).tick()
    result = next(
        t for t in service.store.get(run_id)["targets"] if t["runtime"] == "gcp_cloud_run"
    )
    assert result["state"] == "uncertain" and result["error_code"] == "release_identity_mismatch"


def test_real_aws_gcp_helm_adapters_accept_shared_build_and_frozen_defaults(
    store, tmp_path, monkeypatch
):
    import hashlib
    from pathlib import Path

    from aws.conftest import NOTION_ENV

    from controlplane.execution.gcp.settings import GcpSettings
    from controlplane.providers.gcp.adapter import GcpAdapter
    from controlplane.providers.onprem.helm.adapter import HelmAdapter
    from controlplane.providers.onprem.helm.settings import HelmSettings
    from oneport_aws.adapter import AwsAdapter
    from oneport_aws.execution.oneport_ecs_runtime import OneportEcsSettings

    env_file = tmp_path / "aws.env"
    env_file.write_text(NOTION_ENV)  # fake credentials from existing test fixtures
    aws = AwsAdapter(
        OneportEcsSettings(
            database_url=store.dsn,
            env_file=env_file,
            application="aws-demo",
            environment_id="real-aws",
            allowed_images=[IMAGE],
        ),
        store=store,
    )
    gcp = GcpAdapter(
        GcpSettings(
            database_url=store.dsn,
            environment={
                "application": "gcp-demo",
                "environment_id": "real-gcp",
                "project_id": "test-project",
                "region": "asia-northeast3",
                "service": "demo",
                "service_uid": "registered-uid",
                "service_url": "https://demo.gcp.example.com",
            },
            allowed_images=[IMAGE],
            data_provisioning={
                "app_id": "gcp-demo",
                "network": "projects/test-project/global/networks/oneport",
                "subnetwork": "projects/test-project/regions/asia-northeast3/subnetworks/oneport-cloud-run",
                "allocated_ip_range": "oneport-private-services",
            },
            cache_provisioning={
                "app_id": "gcp-demo",
                "network": "projects/test-project/global/networks/oneport",
                "subnetwork": "projects/test-project/regions/asia-northeast3/subnetworks/oneport-cloud-run",
            },
        ),
        store=store,
    )
    charts = {}
    for field, name, version in [
        ("app_chart", "oneport-app", "0.3.0"),
        ("data_chart", "oneport-data", "0.1.1"),
    ]:
        path = Path(__file__).parents[2] / "vendor/helm" / f"{name}-{version}.tgz"
        charts[field] = {
            "name": name,
            "version": version,
            "path": path,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    helm = HelmAdapter(
        HelmSettings(
            database_url=store.dsn,
            kubeconfig=tmp_path / "kubeconfig",
            context="test",
            server="https://cluster.example",
            binary=tmp_path / "helm",
            demo_database=False,
            environment={
                "application": "helm-demo",
                "environment_id": "real-helm",
                "hostname": "demo.yubin.website",
            },
            allowed_images=[IMAGE],
            **charts,
        ),
        store=store,
    )
    registry = ProviderRegistry()
    for adapter in (aws, gcp, helm):
        registry.register(adapter)
    registry.enable_catalog(store)
    registry.initialize()
    aws.register_application(port=8080, health_path="/health")
    settings = RunSettings(targets={a.runtime: a.environment_id for a in (aws, gcp, helm)})
    builds = BuildSettings(
        project="oneport-build", image_repository="docker.io/example/demo", repositories=[REPO]
    )
    BuildStore(store.dsn).migrate()
    RunStore(store.dsn).migrate()
    resolver = Mock()
    resolver.resolve.return_value = COMMIT
    service = RunService(settings, builds, DeploymentService(registry, store), resolver)
    run_id, _ = built((service, None), monkeypatch)
    RunWorker(service).tick()
    targets = service.store.get(run_id)["targets"]
    assert all(t["state"] == "queued" and t["release_id"] for t in targets), targets
    for target in targets:
        row = store.get(target["release_id"])
        assert row["request"]["source"]["image"] == BUILT
        assert row["request"]["rollout"]["promotion"] == "automatic"


def test_preview_rejects_repository_bridge_before_starting_shared_image_build(setup, monkeypatch):
    service, _ = setup
    adapter = service.deployments.registry.get("gcp")
    capabilities = adapter.capabilities().model_copy(update={"source_kinds": ["repository"]})
    monkeypatch.setattr(adapter, "capabilities", lambda: capabilities)
    p = preview(setup)
    target = next(t for t in p["targets"] if t["runtime"] == "gcp_cloud_run")
    assert not p["ready"] and target["blockers"] == ["target_image_deploy_unsupported"]
    assert not BuildStore(service.store.dsn).list()


def test_new_deployment_invalidates_preview_before_build(setup):
    from controlplane.providers.interface import DeploymentSpec

    service, _ = setup
    p = preview(setup)
    spec = next(t["spec"] for t in p["targets"] if t["runtime"] == "gcp_cloud_run")
    manual = service.deployments.submit(
        DeploymentSpec.model_validate(spec), "manual-after-preview-001", TEAM
    )
    with service.store.connect() as conn:
        conn.execute("UPDATE releases SET state='succeeded' WHERE id=%s", (manual.id,))
    with pytest.raises(RequestConflict):
        service.start(StartRun(preview_id=p["id"], idempotency_key="stale-preview-000001"), TEAM)
    assert not BuildStore(service.store.dsn).list()


def test_operator_grant_revoked_after_environment_owner_changes(setup, monkeypatch):
    from controlplane.demo_deployments.models import DemoDeployInput

    service, _ = setup
    run = service.launch(
        DemoDeployInput(repository=REPO, idempotency_key="operator-owner-0001"), None
    )
    RunWorker(service).tick()
    with service.store.connect() as conn:
        conn.execute("UPDATE environment_teams SET team='beta' WHERE environment_id='gcp'")
    # The operator's frozen owner is alpha; using an admin identity must not silently
    # widen an old artifact grant to a different team after target reassignment.
    with pytest.raises(RequestConflict):
        service.validate(service.store.get(run["id"])["snapshot"], None, runtimes=["gcp_cloud_run"])


@pytest.mark.parametrize("catalog_app", [False, True])
def test_unlisted_public_repository_previews_starts_and_revalidates(setup, catalog_app):
    service, app_id = setup
    if catalog_app:
        service.settings = RunSettings()
    repository = "https://github.com/another-team/public-app"
    p = service.preview(
        PreviewInput(
            repository=repository,
            revision="1.2.1",
            application_id=app_id if catalog_app else None,
        ),
        TEAM,
    )
    assert p["ready"], p
    service.resolver.resolve.assert_called_once_with(repository, "1.2.1")
    # Changes to optional suggestions cannot invalidate an approved public source.
    service.build_settings.repositories = []
    run = service.start(StartRun(preview_id=p["id"], idempotency_key="unlisted-source-0001"), TEAM)
    snapshot = service.store.get(run["id"])["snapshot"]
    assert snapshot["source"]["repository"] == repository
    service.validate(snapshot, TEAM)
    snapshot["source"]["repository"] = "https://outside.test/team/app"
    with pytest.raises(RequestConflict, match="GitHub"):
        service.validate(snapshot, TEAM)


def test_default_branch_resolution_accepts_unlisted_public_repository(setup, monkeypatch):
    from controlplane.builds.provider import CodeBuild
    from controlplane.demo_deployments.source import DemoSource

    service, _ = setup
    response = Mock(status_code=200)
    response.json.return_value = {"default_branch": "main"}
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock()
    client.get.return_value = response
    monkeypatch.setattr(
        "controlplane.demo_deployments.source.httpx.Client", lambda **kwargs: client
    )
    resolve = Mock(return_value=COMMIT)
    monkeypatch.setattr(CodeBuild, "resolve", resolve)
    repository = "https://github.com/another-team/public-app"
    assert DemoSource(service.build_settings).resolve(repository + ".git/", None) == COMMIT
    assert client.get.call_args.args[0] == "https://api.github.com/repos/another-team/public-app"
    resolve.assert_called_once_with(repository, "main")
