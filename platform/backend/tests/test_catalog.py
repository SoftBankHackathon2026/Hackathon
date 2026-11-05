"""Persistent app identity, target sharing and side-effect-free planning."""

import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from controlplane.core.errors import RequestConflict
from controlplane.main import create_app
from controlplane.providers.interface import DeploymentSpec
from controlplane.providers.onprem.helm.adapter import HelmAdapter
from controlplane.providers.onprem.helm.settings import HelmSettings
from controlplane.providers.registry import ProviderRegistry
from controlplane.schemas.accounts import Identity
from controlplane.schemas.catalog import CreateApplication
from controlplane.services.deployments import DeploymentService


@pytest.fixture
def catalog(store, tmp_path):
    charts = {}
    packages = Path(__file__).parents[2] / "vendor/helm"
    for field, name in [("app_chart", "oneport-app"), ("data_chart", "oneport-data")]:
        path = packages / f"{name}-0.1.1.tgz"
        charts[field] = dict(
            name=name,
            version="0.1.1",
            path=path,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    settings = HelmSettings(
        database_url=SecretStr(store.dsn),
        kubeconfig=tmp_path / "config",
        context="test",
        server="https://cluster.example",
        binary=tmp_path / "helm",
        environment=dict(
            application="existing", environment_id="old-onprem", hostname="existing.yubin.website"
        ),
        allowed_images=["docker.io/team/demo@sha256:" + "a" * 64],
        catalog_target_id="onprem-main",
        catalog_max_apps=3,
        **charts,
    )
    registry = ProviderRegistry()
    registry.register(HelmAdapter(settings, store=store))
    registry.enable_catalog(store)
    registry.initialize()
    return registry.catalog


def new_app(catalog, name="notes", team="alpha"):
    return catalog.create(
        CreateApplication(name=name, idempotency_key="create-application-" + name + team),
        Identity(email=team + "@example.com", team=team),
    )


def test_create_does_not_make_environment_or_release_and_is_idempotent(catalog):
    first = new_app(catalog)
    assert new_app(catalog) == first
    app = next(a for a in catalog.list_applications() if a["id"] == first["id"])
    assert app["environments"] == []
    assert catalog.store.list() == []
    with pytest.raises(RequestConflict, match="이미"):
        catalog.create(
            CreateApplication(name="notes", idempotency_key="different-creation-key"),
            Identity(email="a@example.com", team="alpha"),
        )


def test_shared_target_keeps_two_apps_isolated_and_survives_restart(catalog):
    a, b = new_app(catalog, "first"), new_app(catalog, "second")
    identity = Identity(email="a@example.com", team="alpha")
    x = catalog.attach(a["id"], "onprem-main", identity)
    y = catalog.attach(b["id"], "onprem-main", identity)
    assert x != y and catalog.attach(a["id"], "onprem-main", identity) == x
    pa, pb = catalog.resolve(x["id"]), catalog.resolve(y["id"])
    assert pa.application != pb.application
    assert pa.settings.environment.hostname != pb.settings.environment.hostname
    for provider, prefix in [(pa, "first"), (pb, "second")]:
        hostname = provider.settings.environment.hostname
        assert hostname.startswith(prefix + "-") and hostname.endswith(".yubin.website")
        assert len(hostname.split(".")[0].removeprefix(prefix + "-")) == 4
        assert len(provider.application.removeprefix(prefix + "-")) == 10
    assert pa.settings.catalog_dynamic
    assert pa.settings.demo_database is False
    assert not pa.settings.environment.secret_refs
    assert catalog.store.list() == []
    catalog.registry.initialize()
    assert catalog.resolve(x["id"]).fingerprint() == pa.fingerprint()
    with catalog.store.connect() as conn:
        payload = conn.execute(
            "SELECT binding::text AS b FROM app_environments WHERE id=%s", (x["id"],)
        ).fetchone()["b"]
    assert (
        catalog.store.dsn not in payload
        and "database_url" not in payload
        and "kubeconfig" not in payload
    )
    assert {e["id"] for e in catalog.registry.environments()} == {"old-onprem", x["id"], y["id"]}


@pytest.mark.parametrize("occupied_lengths", [(4,), (4, 5), (4, 5, 6)])
def test_short_hostname_collisions_never_reuse_an_existing_address(catalog, occupied_lengths):
    app = new_app(catalog)
    suffix = hashlib.sha256((app["id"] + "\0onprem-main").encode()).hexdigest()[:10]
    occupied = []
    for length in occupied_lengths:
        reserved = new_app(catalog, f"reserved-{length}")
        hostname = f"notes-{suffix[:length]}.yubin.website"
        with catalog.store.connect() as conn:
            conn.execute(
                "INSERT INTO app_environments(id,application_id,target_id,runtime_application,hostname) VALUES(%s,%s,%s,%s,%s)",
                (f"reserved-{length}", reserved["id"], "onprem-main", reserved["name"], hostname),
            )
        occupied.append(hostname)

    if len(occupied_lengths) == 3:
        with pytest.raises(RequestConflict, match="짧은 주소"):
            catalog.attach(app["id"], "onprem-main")
        assert (
            next(a for a in catalog.list_applications() if a["id"] == app["id"])["environments"]
            == []
        )
    else:
        binding = catalog.attach(app["id"], "onprem-main")
        hostname = catalog.binding(binding["id"])["hostname"]
        assert hostname not in occupied
        assert hostname == f"notes-{suffix[: 4 + len(occupied_lengths)]}.yubin.website"
        assert catalog.attach(app["id"], "onprem-main") == binding

    for length, hostname in zip(occupied_lengths, occupied):
        assert catalog.binding(f"reserved-{length}")["hostname"] == hostname


def test_existing_ten_character_hostname_survives_reattach_and_restart(catalog):
    app = new_app(catalog)
    binding = catalog.attach(app["id"], "onprem-main")
    row = catalog.binding(binding["id"])
    legacy_hostname = row["runtime_application"] + ".yubin.website"
    with catalog.store.connect() as conn:
        conn.execute(
            "UPDATE app_environments SET hostname=%s, binding=jsonb_set(binding,'{environment,hostname}',to_jsonb(%s::text)) WHERE id=%s",
            (legacy_hostname, legacy_hostname, binding["id"]),
        )
    previous = catalog.resolve(binding["id"]).fingerprint()
    assert catalog.attach(app["id"], "onprem-main") == binding
    catalog.registry.initialize()
    provider = catalog.resolve(binding["id"])
    assert provider.settings.environment.hostname == legacy_hostname
    assert provider.application == row["runtime_application"]
    assert provider.fingerprint() == previous


def test_app_owner_not_entire_target_controls_access(catalog):
    alpha = Identity(email="a@example.com", team="alpha")
    beta = Identity(email="b@example.com", team="beta")
    a, b = new_app(catalog, "first"), new_app(catalog, "second", "beta")
    x = catalog.attach(a["id"], "onprem-main", alpha)
    y = catalog.attach(b["id"], "onprem-main", beta)
    with pytest.raises(RequestConflict):
        catalog.attach(a["id"], "onprem-main", beta)
    adapter = catalog.resolve(x["id"])
    spec = DeploymentSpec.model_validate(adapter.settings.demo_request().model_dump(mode="json"))
    service = DeploymentService(catalog.registry, catalog.store)
    assert service.plan(spec, alpha).environment_id == x["id"]
    with pytest.raises(RequestConflict):
        service.plan(spec, beta)
    visible = service.visible_environments(catalog.registry.environments(), beta)
    assert x["id"] not in [e["id"] for e in visible] and y["id"] in [e["id"] for e in visible]
    with catalog.store.connect() as conn:
        assert not conn.execute("SELECT * FROM environment_teams").fetchall()


def test_app_counts_are_unlimited_with_legacy_target_limit(catalog):
    # The fixture retains catalog_max_apps=3, as existing runtime files/bindings do.
    bindings = [
        catalog.attach(new_app(catalog, f"app-{index}")["id"], "onprem-main") for index in range(51)
    ]
    assert len({binding["id"] for binding in bindings}) == 51
    assert len(catalog.list_applications()) == 52  # Includes the existing imported application.
    catalog.registry.initialize()
    assert catalog.resolve(bindings[-1]["id"]) is not None
    assert catalog.store.list() == []
    old = catalog.binding("old-onprem")
    assert old["binding"] is None and old["hostname"] == "existing.yubin.website"
    assert catalog.registry.get("old-onprem").application == "existing"


def test_legacy_app_limit_does_not_change_binding_fingerprint(catalog):
    settings = catalog.registry.get("old-onprem").settings
    payload = settings.model_dump()
    payload.pop("catalog_max_apps")
    without_limit = HelmSettings.model_validate(payload)
    assert without_limit.catalog_max_apps is None
    assert without_limit.fingerprint() == settings.fingerprint()


def test_plan_has_no_release_and_changed_plan_is_not_submitted(catalog):
    token = "catalog-test-token-with-32-characters"
    with TestClient(
        create_app(registry=catalog.registry, store=catalog.store, operator_token=token)
    ) as c:
        headers = {"Authorization": "Bearer " + token}
        created = c.post(
            "/api/v1/applications",
            headers=headers,
            json={"name": "api-app", "idempotency_key": "catalog-api-create-12345"},
        )
        assert created.status_code == 201, created.text
        env = c.post(
            f"/api/v1/applications/{created.json()['id']}/environments",
            headers=headers,
            json={"target_id": "onprem-main"},
        ).json()
        request = env["request_template"]
        request["strategy"] = "canary"
        planned = c.post("/api/v1/plans", json=request)
        assert planned.status_code == 200, planned.text
        assert catalog.store.list() == []
        response = c.post(
            "/api/v1/releases",
            headers=headers,
            json={
                "request": request,
                "idempotency_key": "catalog-api-deploy-12345",
                "plan_fingerprint": "f" * 64,
            },
        )
        assert response.status_code == 409 and catalog.store.list() == []
        response = c.post(
            "/api/v1/releases",
            headers=headers,
            json={
                "request": request,
                "idempotency_key": "catalog-api-deploy-12345",
                "plan_fingerprint": planned.json()["fingerprint"],
            },
        )
        assert response.status_code == 202, response.text
        assert len(catalog.store.list()) == 1
        assert (
            c.post(
                "/api/v1/releases",
                headers=headers,
                json={
                    "request": request,
                    "idempotency_key": "catalog-api-deploy-12345",
                    "plan_fingerprint": planned.json()["fingerprint"],
                },
            ).json()["id"]
            == response.json()["id"]
        )


def test_target_worker_discovers_new_apps_without_restart(catalog, monkeypatch):
    from controlplane.providers.onprem.helm.catalog_worker import CatalogHelmWorker
    from controlplane.providers.onprem.helm.runtime import HelmRuntime

    tick = Mock()
    monkeypatch.setattr("controlplane.providers.onprem.helm.catalog_worker.HelmWorker.tick", tick)
    base = catalog.registry.get("old-onprem")
    worker = CatalogHelmWorker(catalog.store, HelmRuntime(base.settings))
    assert worker.workers == {}
    x = catalog.attach(new_app(catalog)["id"], "onprem-main")
    worker.tick()
    assert x["id"] in worker.workers and tick.call_count == 2
    original = worker.workers[x["id"]]
    worker.tick()
    assert worker.workers[x["id"]] is original
    worker.close()


def test_legacy_claim_is_visible_without_restarting_or_recreating_app(catalog):
    from controlplane.repositories.accounts import AccountStore

    alpha = Identity(email="a@example.com", team="alpha")
    beta = Identity(email="b@example.com", team="beta")
    app_id = catalog.binding("old-onprem")["application_id"]
    assert app_id in [a["id"] for a in catalog.list_applications(alpha)]
    AccountStore(catalog.store).claim("old-onprem", alpha)
    assert catalog.application(app_id, alpha)["team"] == "alpha"
    assert app_id not in [a["id"] for a in catalog.list_applications(beta)]
    catalog.bootstrap()
    assert catalog.binding("old-onprem")["application_id"] == app_id
    assert len(catalog.list_applications()) == 1


def test_new_app_requires_source_and_redeploy_reuses_successful_request(catalog):
    app = new_app(catalog)
    bound = catalog.attach(app["id"], "onprem-main")
    provider = catalog.resolve(bound["id"])
    env = provider.public_environment()
    assert env["images"] == [] and env["request_template"]["source"]["image"].endswith("0" * 64)
    request = provider.settings.demo_request()
    request.source.image = "docker.io/team/service@sha256:" + "a" * 64
    request.port = 9000
    request.health_path = "/ready"
    row = catalog.store.enqueue(
        request, "catalog-previous-success", provider.fingerprint(), runtime=provider.runtime
    )
    with catalog.store.connect() as conn:
        conn.execute("UPDATE releases SET state='succeeded' WHERE id=%s", (row["id"],))
    template = provider.public_environment()["request_template"]
    assert template["source"]["image"] == request.source.image
    assert template["port"] == 9000 and template["health_path"] == "/ready"


def test_activation_after_initial_import_keeps_target_id_and_dispatches(catalog, monkeypatch):
    from controlplane.providers.onprem.helm.catalog_worker import CatalogHelmWorker
    from controlplane.providers.onprem.helm.runtime import HelmRuntime

    base = catalog.registry.get("old-onprem")
    # API may import the old config before CD activates the new target setting.
    base.settings.catalog_target_id = None
    catalog.bootstrap()
    assert catalog.targets()[0]["creation_enabled"] is False
    base.settings.catalog_target_id = "new-preferred-target-id"
    catalog.bootstrap()
    assert catalog.targets()[0]["id"] == "onprem-main"
    assert catalog.targets()[0]["creation_enabled"] is True
    binding = catalog.attach(new_app(catalog)["id"], "onprem-main")
    tick = Mock()
    monkeypatch.setattr("controlplane.providers.onprem.helm.catalog_worker.HelmWorker.tick", tick)
    worker = CatalogHelmWorker(catalog.store, HelmRuntime(base.settings))
    worker.tick()
    assert binding["id"] in worker.workers and tick.call_count == 2
    worker.close()


def test_catalog_does_not_wait_for_source_build_lock(catalog, monkeypatch):
    connect = catalog.store.connect

    def bounded_connect():
        conn = connect()
        conn.execute("SET lock_timeout = '500ms'")
        return conn

    monkeypatch.setattr(catalog.store, "connect", bounded_connect)
    with connect() as build:
        build.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()),4)")
        app = new_app(catalog)
        binding = catalog.attach(app["id"], "onprem-main")
        assert binding["id"].startswith("ae-")
