"""Crash boundaries, credential retention and the private-to-public release path."""

import base64
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from helm_fakes import rollout_state
from kubernetes.client.exceptions import ApiException

from controlplane.domain.models import Runtime, Strategy
from controlplane.providers.contracts import ProviderConflict, ReleaseKey
from controlplane.providers.onprem.helm import data
from controlplane.providers.onprem.helm.models import HelmReceipt, HelmRelease
from controlplane.providers.onprem.helm.runtime import HelmRuntime
from controlplane.providers.onprem.helm.settings import HelmSettings
from controlplane.providers.onprem.helm.worker import HelmWorker


@pytest.fixture
def settings(tmp_path):
    packages = Path(__file__).parents[2] / "vendor/helm"
    charts = {}
    for field, name in [("app_chart", "oneport-app"), ("data_chart", "oneport-data")]:
        path = packages / (name + "-0.1.1.tgz")
        charts[field] = dict(
            path=path,
            name=name,
            version="0.1.1",
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return HelmSettings(
        database_url="postgresql://localhost/test",
        kubeconfig=tmp_path / "kubeconfig",
        context="test",
        server="https://cluster.example",
        binary=tmp_path / "helm",
        environment=dict(
            application="helm-demo", environment_id="helm-dev", hostname="helm-demo.yubin.website"
        ),
        allowed_images=["docker.io/team/demo@sha256:" + "a" * 64],
        **charts,
    )


def missing(*args, **kwargs):
    raise ApiException(status=404)


@pytest.fixture
def clients():
    c = NS(core=Mock(), apps=Mock())
    for call in [
        c.core.read_namespaced_secret,
        c.core.read_namespaced_persistent_volume_claim,
        c.apps.read_namespaced_stateful_set,
        c.apps.read_namespaced_deployment,
    ]:
        call.side_effect = missing

    def create(namespace, body, **kwargs):
        result = NS(
            type=body["type"],
            immutable=body["immutable"],
            metadata=NS(
                uid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                labels=body["metadata"]["labels"],
                annotations=body["metadata"]["annotations"],
            ),
            data={k: base64.b64encode(v.encode()).decode() for k, v in body["stringData"].items()},
        )
        c.core.read_namespaced_secret.side_effect = None
        c.core.read_namespaced_secret.return_value = result
        return result

    c.core.create_namespaced_secret.side_effect = create
    return c


def test_credentials_reused_after_lost_response_and_never_in_values(settings, clients):
    req = settings.demo_request()
    first = data.prepare(clients, settings.environment, req, "intent-a")
    secret = deepcopy(clients.core.read_namespaced_secret.return_value.data)
    assert data.prepare(clients, settings.environment, req, "intent-a") == first
    assert data.prepare(clients, settings.environment, req, "next-release", first) == first
    assert clients.core.create_namespaced_secret.call_count == 1
    for value in secret.values():
        assert base64.b64decode(value).decode() not in json.dumps(first)
    with pytest.raises(ProviderConflict, match="different creation intent"):
        data.prepare(clients, settings.environment, req, "different-intent")
    assert clients.core.read_namespaced_secret.return_value.data == secret


@pytest.mark.parametrize("remaining", ["pvc", "postgres", "redis"])
def test_orphan_data_blocks_password_regeneration(settings, clients, remaining):
    call = {
        "pvc": clients.core.read_namespaced_persistent_volume_claim,
        "postgres": clients.apps.read_namespaced_stateful_set,
        "redis": clients.apps.read_namespaced_deployment,
    }[remaining]
    call.side_effect = None
    call.return_value = NS()
    with pytest.raises(ProviderConflict, match="recovery required"):
        data.prepare(clients, settings.environment, settings.demo_request(), "new-intent")
    clients.core.create_namespaced_secret.assert_not_called()


def test_recorded_missing_or_replaced_secret_is_never_recreated(settings, clients):
    request = settings.demo_request()
    saved = data.prepare(clients, settings.environment, request, "intent")
    clients.core.read_namespaced_secret.return_value.metadata.uid = "replacement"
    with pytest.raises(ProviderConflict, match="UID changed"):
        data.prepare(clients, settings.environment, request, "retry", saved)
    clients.core.read_namespaced_secret.side_effect = missing
    with pytest.raises(ProviderConflict, match="missing"):
        data.prepare(clients, settings.environment, request, "retry", saved)
    assert clients.core.create_namespaced_secret.call_count == 1


def test_private_app_plan_binds_data_by_uid_not_plaintext(settings, clients):
    request = settings.demo_request()
    values = data.prepare(clients, settings.environment, request, "intent")
    plan = HelmRuntime(settings).app_plan(request, ReleaseKey(value="a" * 24), values, False)
    assert plan.values["route"]["enabled"] is False
    assert plan.values["secretBindings"] == [
        dict(
            env="DATABASE_URL",
            name=values["credentialsSecret"],
            key="DATABASE_URL",
            uid=values["credentialsSecretUid"],
        )
    ]
    assert request.resources.database_mode == "container"
    assert request.secret_bindings == []
    assert "postgresql://" not in json.dumps(plan.values)


def test_old_test_job_is_not_evidence_of_current_test(settings):
    runtime = HelmRuntime(settings)
    job = NS(
        metadata=NS(uid="old", labels={"app.kubernetes.io/instance": "release"}),
        status=NS(succeeded=1, failed=None),
    )
    runtime.clients = NS(batch=NS(read_namespaced_job=Mock(return_value=job)))
    receipt = NS(release="release")
    assert runtime.test_result(receipt, previous_uid="old") == (None, None)
    job.metadata.uid = "new"
    assert runtime.test_result(receipt, previous_uid="old") == (True, "new")
    with pytest.raises(ProviderConflict, match="UID changed"):
        runtime.test_result(receipt, expected_uid="other")


class FakeHelm:
    def __init__(self):
        self.history = {}
        self.revision = 0
        self.applies = []
        self.tests = 0

    def current(self, release):
        if not self.revision:
            return None
        return HelmRelease(revision=self.revision, status="deployed")

    def values(self, release, revision):
        return self.history[revision].values

    def observe_receipt(self, plan, revision):
        return HelmReceipt(
            release=plan.release,
            revision=revision,
            chart_sha256=plan.chart_sha256,
            values_fingerprint=plan.fingerprint,
        )

    def apply(self, plan, expected_revision):
        assert expected_revision == (self.revision or None)
        self.revision += 1
        self.history[self.revision] = plan
        self.applies.append(deepcopy(plan.values))
        return self.observe_receipt(plan, self.revision)

    def test(self, receipt):
        self.tests += 1

    def rollback(self, plan, receipt, expected_revision):
        return self.apply(plan, expected_revision)


@pytest.fixture
def workflow(settings, store):
    settings.database_url = settings.database_url.__class__(store.dsn)
    runtime = HelmRuntime(settings)
    runtime.app = FakeHelm()
    runtime.connect = lambda: None
    runtime.verify_hostname = lambda: None
    runtime.observe = lambda plan, uid=None: rollout_state(
        uid="rollout-uid", generation=1, ready=True, failed=False
    )
    runtime.test_result = lambda receipt, expected_uid=None, previous_uid=None: (
        True,
        str(runtime.app.tests),
    )
    runtime.probe = lambda plan: True
    worker = HelmWorker(store, runtime)
    request = settings.demo_request()
    request.resources.database = "none"
    request.migration = None

    def enqueue(key):
        return store.enqueue(request, key, settings.fingerprint(), runtime=Runtime.ONPREM_HELM)

    def step(row):
        # Direct state-machine call; image resolution has separate registry tests.
        with store.worker_lock() as conn:
            worker._step(conn, store.get(row["id"]) | {"expired": False})
        return store.get(row["id"])

    return NS(
        runtime=runtime, worker=worker, request=request, enqueue=enqueue, step=step, store=store
    )


def finish(w, row):
    for _ in range(20):
        row = w.step(row)
        if row["state"] in {"succeeded", "failed", "uncertain"}:
            return row
    pytest.fail("workflow did not terminate")


def test_first_release_is_private_until_test_then_public_and_reuses_name(workflow):
    w = workflow
    first = finish(w, w.enqueue("first-helm-request-123"))
    assert first["state"] == "succeeded"
    assert [p["route"]["enabled"] for p in w.runtime.app.applies] == [False, True]
    assert w.runtime.app.tests == 1
    second = finish(w, w.enqueue("second-helm-request-123"))
    assert second["state"] == "succeeded"
    assert second["checkpoint"]["previous"]["receipt"]["revision"] == 2
    assert w.runtime.app.revision == 3


def test_crash_after_helm_apply_recovers_without_second_upgrade(workflow):
    w = workflow
    row = w.enqueue("crash-helm-request-123")
    while row["state"] != "app_ready":
        row = w.step(row)
    real = w.runtime.app.apply

    def lost(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("lost process after committed Helm write")

    w.runtime.app.apply = lost
    with pytest.raises(RuntimeError):
        w.step(row)
    assert w.store.get(row["id"])["state"] == "app_submitting"
    w.runtime.app.apply = real
    row = w.step(w.store.get(row["id"]))
    assert row["state"] == "app_waiting"
    assert len(w.runtime.app.applies) == 1
    assert finish(w, row)["state"] == "succeeded"


def test_rollback_revision_becomes_next_verified_base(workflow):
    w = workflow
    first = finish(w, w.enqueue("baseline-helm-request-123"))
    row = w.enqueue("failure-helm-request-123")
    while row["state"] != "app_waiting":
        row = w.step(row)
    healthy = w.runtime.observe
    w.runtime.observe = lambda *a: rollout_state(
        uid="rollout-uid", generation=2, ready=False, failed=True
    )
    row = w.step(row)
    assert row["state"] == "restore_checking"
    w.runtime.observe = healthy
    row = finish(w, row)
    assert row["state"] == "failed"
    assert row["error_code"] == "release_failed_restored"
    assert row["checkpoint"]["app_plan"] == first["checkpoint"]["app_plan"]
    assert row["checkpoint"]["app_receipt"]["revision"] == 4
    assert finish(w, w.enqueue("after-rollback-request-123"))["state"] == "succeeded"


def test_restored_chart_remains_valid_after_another_binding_upgrade(workflow, monkeypatch):
    from test_server_provider_activation import activation

    from controlplane.providers.onprem.helm.models import HelmPlan

    w = workflow
    old = w.runtime.settings
    baseline = finish(w, w.enqueue("review-baseline-request-123"))
    assert baseline["state"] == "succeeded"
    archive = Path(__file__).parents[2] / "vendor/helm/oneport-app-0.2.0.tgz"
    config = old.model_dump(mode="json")
    config["app_chart"] = dict(
        name="oneport-app",
        version="0.2.0",
        path=str(archive),
        sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
    )
    upgraded, changed = activation.reviewed_helm_upgrade(
        config, old.model_dump(mode="json"), w.store.dsn
    )
    assert changed
    first = HelmSettings.model_validate(upgraded | {"database_url": w.store.dsn})
    w.runtime.settings = first
    monkeypatch.setattr(
        w.runtime,
        "rollback_app",
        lambda plan, receipt, *, expected_revision: w.runtime.app.rollback(
            plan, receipt, expected_revision
        ),
    )
    row = w.store.enqueue(
        w.request, "review-failed-upgrade-123", first.fingerprint(), runtime=Runtime.ONPREM_HELM
    )
    while row["state"] != "app_waiting":
        row = w.step(row)
    healthy = w.runtime.observe
    w.runtime.observe = lambda *a: rollout_state(
        uid="rollout-uid", generation=2, ready=False, failed=True
    )
    row = w.step(row)
    assert row["state"] == "restore_checking"
    w.runtime.observe = healthy
    restored = finish(w, row)
    assert restored["error_code"] == "release_failed_restored"
    assert restored["binding_fingerprint"] == first.fingerprint()
    plan = HelmPlan.model_validate(restored["checkpoint"]["app_plan"])
    assert plan.chart_sha256 == old.app_chart.sha256

    second_config = upgraded
    # Configuration-only changes must not evict an older live rollback revision.
    for version in range(16):
        next_config = deepcopy(second_config)
        next_config["observability"] = {"trace_sample_ratio": (version + 1) / 20}
        with w.store.configuration_lock():
            second_config, changed = activation.reviewed_helm_upgrade(
                next_config, second_config, w.store.dsn
            )
        assert changed
        second = HelmSettings.model_validate(second_config | {"database_url": w.store.dsn})
        assert len(second.previous_bindings) <= 3
    w.runtime.settings = second
    w.runtime.verify_previous_binding(restored["binding_fingerprint"], plan)
    row = w.store.enqueue(
        w.request, "review-after-restore-123", second.fingerprint(), runtime=Runtime.ONPREM_HELM
    )
    assert finish(w, row)["state"] == "succeeded"
    # Retaining rollback charts must not approve a newer chart under an older binding.
    newer_plan = w.runtime.app_plan(w.request, ReleaseKey(value="a" * 24), None, True)
    with pytest.raises(ProviderConflict, match="pair was not approved"):
        w.runtime.verify_previous_binding(old.fingerprint(), newer_plan)
    again, changed = activation.reviewed_helm_upgrade(next_config, second_config, w.store.dsn)
    assert not changed and again == second_config


def test_many_successful_upgrades_do_not_exhaust_binding_history(workflow):
    from test_server_provider_activation import activation

    w = workflow
    assert finish(w, w.enqueue("before-many-upgrades-123"))["state"] == "succeeded"
    archive = Path(__file__).parents[2] / "vendor/helm/oneport-app-0.2.0.tgz"
    previous = w.runtime.settings.model_dump(mode="json")
    config = deepcopy(previous)
    config["app_chart"] = dict(
        name="oneport-app",
        version="0.2.0",
        path=str(archive),
        sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
    )
    for version in range(16):
        config["chart_source_commit"] = f"{version:040x}"
        with w.store.configuration_lock():
            upgraded, changed = activation.reviewed_helm_upgrade(config, previous, w.store.dsn)
        assert changed
        settings = HelmSettings.model_validate(upgraded | {"database_url": w.store.dsn})
        w.runtime.settings = settings
        row = w.store.enqueue(
            w.request,
            f"many-upgrades-{version:04}",
            settings.fingerprint(),
            runtime=Runtime.ONPREM_HELM,
        )
        assert finish(w, row)["state"] == "succeeded"
        assert len(settings.previous_bindings) <= 3
        previous = upgraded


def test_catalog_metadata_lock_does_not_reject_deployments(workflow):
    w = workflow
    with w.store.connect() as catalog:
        catalog.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), 5)")
        assert w.enqueue("during-catalog-write-123")["state"] == "queued"


def test_binding_compaction_preserves_data_from_a_failed_release_without_an_app_plan(workflow):
    from test_server_provider_activation import activation

    w = workflow
    original = w.runtime.settings
    baseline = finish(w, w.enqueue("before-data-failure-123"))
    archive = Path(__file__).parents[2] / "vendor/helm/oneport-app-0.2.0.tgz"
    config = original.model_dump(mode="json")
    config["app_chart"] = dict(
        name="oneport-app",
        version="0.2.0",
        path=str(archive),
        sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
    )
    upgraded, _ = activation.reviewed_helm_upgrade(
        config, original.model_dump(mode="json"), w.store.dsn
    )
    first = HelmSettings.model_validate(upgraded | {"database_url": w.store.dsn})
    w.request.resources.database = "postgresql"
    failed = w.store.enqueue(
        w.request, "failed-after-data-123", first.fingerprint(), runtime=Runtime.ONPREM_HELM
    )
    data = {"credentialsSecret": "existing-secret", "credentialsSecretUid": "original-secret-uid"}
    checkpoint = {"data_values": data, "data_uids": {"postgresql": "original-pvc-uid"}}
    with w.store.connect() as conn:
        w.store.transition(
            conn,
            failed,
            "failed",
            "migration failed",
            checkpoint=checkpoint,
            error_code="migration_failed",
        )
    # A newer record from another provider/app/environment must not replace these roots.
    for index, (field, runtime) in enumerate(
        [
            (None, Runtime.ONPREM_ARGO),
            ("application", Runtime.ONPREM_HELM),
            ("environment_id", Runtime.ONPREM_HELM),
        ]
    ):
        request = w.request.model_copy(update={field: "foreign-app"} if field else {})
        foreign = w.store.enqueue(
            request, f"foreign-data-journal-{index}", first.fingerprint(), runtime=runtime
        )
        with w.store.connect() as conn:
            w.store.transition(
                conn, foreign, "failed", "unrelated", checkpoint={"data_values": {"foreign": True}}
            )
    for version in range(16):
        config["chart_source_commit"] = f"{version:040x}"
        with w.store.configuration_lock():
            upgraded, _ = activation.reviewed_helm_upgrade(config, upgraded, w.store.dsn)
        settings = HelmSettings.model_validate(upgraded | {"database_url": w.store.dsn})
        assert len(settings.previous_bindings) <= 3
        assert original.fingerprint() in settings.accepted_bindings()
        assert first.fingerprint() in settings.accepted_bindings()
    w.runtime.settings = settings
    row = w.store.enqueue(
        w.request, "after-data-compaction-123", settings.fingerprint(), runtime=Runtime.ONPREM_HELM
    )
    row = w.step(row)
    assert row["state"] == "data_preparing"
    assert row["checkpoint"]["data_values"] == data
    assert row["checkpoint"]["data_uids"] == checkpoint["data_uids"]
    assert row["checkpoint"]["previous"]["plan"] == baseline["checkpoint"]["app_plan"]
    assert w.store.get(baseline["id"])["checkpoint"] == baseline["checkpoint"]
    assert w.store.get(failed["id"])["checkpoint"] == checkpoint


def test_binding_compaction_does_not_approve_an_unreviewed_journal_chart(workflow):
    from psycopg.types.json import Jsonb
    from test_server_provider_activation import activation

    w = workflow
    baseline = finish(w, w.enqueue("before-invalid-journal-123"))
    settings = w.runtime.settings
    archive = Path(__file__).parents[2] / "vendor/helm/oneport-app-0.2.0.tgz"
    config = settings.model_dump(mode="json")
    config["app_chart"] = dict(
        name="oneport-app",
        version="0.2.0",
        path=str(archive),
        sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
    )
    checkpoint = deepcopy(baseline["checkpoint"])
    checkpoint["app_plan"]["chart_sha256"] = config["app_chart"]["sha256"]
    with w.store.connect() as conn:
        conn.execute(
            "UPDATE releases SET checkpoint=%s WHERE id=%s", (Jsonb(checkpoint), baseline["id"])
        )
    with w.store.configuration_lock(), pytest.raises(ValueError, match="unapproved Helm"):
        activation.reviewed_helm_upgrade(config, settings.model_dump(mode="json"), w.store.dsn)


def test_configuration_switch_closes_the_gap_after_the_idle_check(workflow):
    from test_server_provider_activation import activation

    from controlplane.core.errors import RequestConflict

    w = workflow
    with w.store.configuration_lock():
        activation.require_idle_helm(w.runtime.settings.model_dump(), w.store.dsn)
        with pytest.raises(RequestConflict, match="설정을 갱신"):
            w.enqueue("submitted-during-switch-123")
        with w.store.worker_lock() as conn:
            assert conn is None
        assert w.store.list() == []
    assert finish(w, w.enqueue("submitted-after-switch-123"))["state"] == "succeeded"


def test_configuration_switch_waits_for_existing_worker_tick(workflow):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    w = workflow
    started, entered, release = Event(), Event(), Event()

    def switch():
        started.set()
        with w.store.configuration_lock():
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with w.store.worker_lock() as conn:
                assert conn is not None
                future = pool.submit(switch)
                assert started.wait(5)
                assert not entered.wait(0.1)
            assert entered.wait(5)
            with w.store.worker_lock() as conn:
                assert conn is None
        finally:
            release.set()
        future.result(timeout=5)


def test_failed_configuration_switch_releases_intake_and_worker_locks(workflow):
    w = workflow
    with pytest.raises(RuntimeError, match="switch failed"):
        with w.store.configuration_lock():
            raise RuntimeError("switch failed")
    assert finish(w, w.enqueue("after-failed-switch-123"))["state"] == "succeeded"


def test_api_accepts_container_database_and_keeps_legacy_provider_separate(settings, store):
    from fastapi.testclient import TestClient

    from controlplane.main import create_app
    from controlplane.providers.onprem.helm.adapter import HelmAdapter
    from controlplane.providers.registry import ProviderRegistry

    settings.database_url = settings.database_url.__class__(store.dsn)
    registry = ProviderRegistry()
    registry.register(HelmAdapter(settings, store=store))
    token = "test-helm-token-at-least-thirty-two-characters"
    with TestClient(create_app(registry=registry, store=store, operator_token=token)) as client:
        env = next(
            e
            for e in client.get("/api/v1/environments").json()
            if e["id"] == settings.environment.environment_id
        )
        assert env["runtime"] == "onprem_helm" and env["engine"] == "helm"
        assert env["request_template"]["resources"]["database_mode"] == "container"
        body = dict(request=env["request_template"], idempotency_key="api-helm-request-12345")
        response = client.post(
            "/api/v1/releases", json=body, headers={"Authorization": "Bearer " + token}
        )
        assert response.status_code == 202, response.text
        assert response.json()["runtime"] == "onprem_helm"
        assert store.get(response.json()["id"])["request"]["secret_bindings"] == []


def test_original_ci_packages_have_matching_metadata_and_sha(settings):
    from controlplane.providers.onprem.helm.client import HelmClient

    for artifact in [settings.app_chart, settings.data_chart]:
        client = HelmClient(
            binary=settings.binary,
            kubeconfig=settings.kubeconfig,
            context=settings.context,
            expected_server=settings.server,
            artifact=artifact,
        )
        assert client.load_package() == artifact.path.read_bytes()


def test_lost_secret_create_response_recovers_exact_creation_intent(settings, clients):
    from controlplane.providers.contracts import SubmissionUncertain

    create = clients.core.create_namespaced_secret.side_effect

    def lost(*args, **kwargs):
        create(*args, **kwargs)
        raise ApiException(status=500)

    clients.core.create_namespaced_secret.side_effect = lost
    with pytest.raises(SubmissionUncertain):
        data.prepare(clients, settings.environment, settings.demo_request(), "saved-intent")
    recovered = data.prepare(clients, settings.environment, settings.demo_request(), "saved-intent")
    assert recovered["credentialsSecretUid"]
    assert clients.core.create_namespaced_secret.call_count == 1


def test_existing_data_choices_survive_page_reload_and_cannot_be_changed(settings, store):
    from controlplane.providers.onprem.helm.adapter import HelmAdapter

    settings.database_url = settings.database_url.__class__(store.dsn)
    adapter = HelmAdapter(settings, store=store)
    request = settings.demo_request()
    request.resources.redis = True
    row = store.enqueue(
        request, "saved-data-request-12345", settings.fingerprint(), runtime=Runtime.ONPREM_HELM
    )
    with store.connect() as conn:
        store.transition(
            conn,
            row,
            "data_waiting",
            "prepared",
            checkpoint={
                "data_values": {"postgresql": {"enabled": True}, "redis": {"enabled": True}}
            },
        )
    env = adapter.public_environment()
    assert env["data_configured"]
    assert env["request_template"]["resources"]["redis"] is True
    request.resources.redis = False
    with pytest.raises(ValueError, match="cannot be changed"):
        adapter.parse_request(request.model_dump(mode="json"))


def test_notes_binding_requires_db_before_spending_on_build(settings):
    settings.required_database = "postgresql"
    request = settings.demo_request()
    request.resources.database = "none"
    request.migration = None
    with pytest.raises(ValueError, match="requires PostgreSQL"):
        settings.validate_request(request)


@pytest.fixture
def migration_workflow(workflow):
    from kubernetes import client
    from kubernetes_fakes import MemoryApi

    w = workflow
    api = MemoryApi()
    serializer = client.ApiClient()
    # Exercise the real Job SDK and its binding checks, with fake K8s transport.
    api.read_namespaced_secret = lambda **kwargs: {
        "metadata": {"uid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
        "immutable": True,
        "data": {"DATABASE_URL": "not-logged"},
    }
    w.runtime.clients = NS(api=serializer, core=api, batch=api, networking=api)
    w.request.resources.database = "postgresql"
    w.request.migration = w.runtime.settings.demo_request().migration
    w.request.env = {"MIGRATION_MODE": "safe"}
    w.api = api

    def prepared():
        row = w.enqueue("helm-migration-regression-123")
        with w.store.connect() as conn:
            w.store.transition(
                conn,
                row,
                "migration_ready",
                "Data release already verified",
                checkpoint={
                    "previous": None,
                    "data_values": {
                        "credentialsSecret": "existing-data-secret",
                        "credentialsSecretUid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    },
                },
            )
        return w.store.get(row["id"])

    w.prepared = prepared
    yield w
    serializer.close()


@pytest.mark.parametrize("strategy", [Strategy.BLUE_GREEN, Strategy.CANARY])
@pytest.mark.parametrize("success", [True, False])
def test_helm_migration_gates_app_without_changing_strategy(migration_workflow, strategy, success):
    w = migration_workflow
    w.request.strategy = strategy
    original = w.request.model_dump(mode="json")
    row = w.step(w.prepared())
    assert row["state"] == "migration_waiting"
    assert w.runtime.app.applies == []
    job = next(iter(w.api.objects.values()))
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == w.request.migration.argv
    assert container["securityContext"]["runAsNonRoot"] is True
    assert {"name": "MIGRATION_MODE", "value": "safe"} in container["env"]
    assert {
        "name": "DATABASE_URL",
        "valueFrom": {"secretKeyRef": {"name": "existing-data-secret", "key": "DATABASE_URL"}},
    } in container["env"]
    job["status"] = {
        "conditions": [{"type": "Complete" if success else "Failed", "status": "True"}],
        "succeeded": int(success),
    }
    row = w.step(row)
    assert row["state"] == ("app_ready" if success else "failed")
    assert row["request"] == original
    assert w.request.model_dump(mode="json") == original
    if success:
        row = finish(w, row)
        assert row["state"] == "succeeded"
        assert all(
            p["strategy"]["type"] == ("canary" if strategy == Strategy.CANARY else "blueGreen")
            for p in w.runtime.app.applies
        )
    else:
        assert row["error_code"] == "migration_failed"
        assert w.runtime.app.applies == []


@pytest.mark.parametrize("replaced", [False, True])
def test_canary_migration_recovers_without_replay_and_retains_uid_guard(
    migration_workflow, replaced
):
    w = migration_workflow
    w.request.strategy = Strategy.CANARY
    row = w.prepared()
    create = w.api.create_namespaced_job

    def lost_response(**kwargs):
        create(**kwargs)
        raise RuntimeError("lost Job create response")

    w.api.create_namespaced_job = lost_response
    with pytest.raises(RuntimeError, match="lost Job"):
        w.step(row)
    row = w.store.get(row["id"])
    assert row["state"] == "migration_submitting"
    w.worker = HelmWorker(w.store, w.runtime)
    with w.store.worker_lock() as conn:
        w.worker._step(conn, row | {"expired": False})
    row = w.store.get(row["id"])
    assert row["state"] == "migration_waiting"
    assert row["checkpoint"]["migration"]["recovered"] is True
    assert w.api.creates == 1
    job = next(iter(w.api.objects.values()))
    job["status"] = {"conditions": [{"type": "Complete", "status": "True"}], "succeeded": 1}
    if replaced:
        job["metadata"]["uid"] = "replacement-job"
        with pytest.raises(ProviderConflict, match="replaced"):
            w.step(row)
    else:
        assert w.step(row)["state"] == "app_ready"
    assert w.api.creates == 1


def test_migration_adapter_keeps_argo_strategy_and_secret_guards(migration_workflow):
    from controlplane.providers.onprem import pods

    w = migration_workflow
    w.request.strategy = Strategy.CANARY
    values = {"credentialsSecret": "existing-data-secret", "credentialsSecretUid": "wrong-uid"}
    bound, env = w.runtime.bound(w.request, values)
    with pytest.raises(ValueError, match="전략"):
        pods.validate(bound, env)
    provider, migration = w.runtime.migration(w.request, values)
    with pytest.raises(ProviderConflict, match="Secret was deleted or replaced"):
        provider.submit_migration(migration, ReleaseKey(value="a" * 24))
    assert w.api.creates == 0


def test_canary_progress_survives_worker_restart_and_finishes(workflow):
    w = workflow
    w.request.strategy = Strategy.CANARY
    finish(w, w.enqueue("canary-first-request-123"))
    row = w.enqueue("canary-second-request-123")
    while row["state"] != "canary_waiting":
        row = w.step(row)
    for step, weight in [(3, 10), (6, 50), (9, 100)]:
        w.runtime.observe = lambda *a: rollout_state(
            uid="rollout-uid",
            generation=2,
            ready=False,
            failed=False,
            observed=True,
            step_index=step,
            canary_weight=weight,
            traffic_uid="traffic-uid",
        )
        row = w.step(row)
        assert row["checkpoint"]["canary_progress"]["canary_weight"] == weight
        w.worker = HelmWorker(w.store, w.runtime)
    w.runtime.observe = lambda *a: rollout_state(
        uid="rollout-uid", generation=2, ready=True, failed=False
    )
    assert finish(w, row)["state"] == "succeeded"
    assert len(w.runtime.app.applies) == 3


def test_canary_traffic_replacement_is_not_accepted(workflow):
    w = workflow
    w.request.strategy = Strategy.CANARY
    row = w.enqueue("canary-identity-request-123")
    while row["state"] != "canary_waiting":
        row = w.step(row)
    uid = "original"
    w.runtime.observe = lambda *a: rollout_state(
        uid="rollout-uid",
        generation=2,
        ready=False,
        failed=False,
        observed=True,
        step_index=3,
        canary_weight=10,
        traffic_uid=uid,
    )
    row = w.step(row)
    uid = "replacement"
    with pytest.raises(ProviderConflict, match="traffic resource was replaced"):
        w.step(row)


def test_canary_rejects_manual_or_disabled_recovery(settings):
    request = settings.demo_request()
    request.strategy = Strategy.CANARY
    from controlplane.domain.models import RolloutPolicy

    request.rollout = RolloutPolicy(promotion="manual")
    with pytest.raises(ValueError, match="automatic"):
        settings.validate_request(request)
    request.rollout = RolloutPolicy(promotion="automatic", auto_rollback=False)
    with pytest.raises(ValueError, match="automatic"):
        settings.validate_request(request)
    request.rollout = RolloutPolicy(promotion="automatic", hold_seconds=5, bake_seconds=30)
    settings.validate_request(request)
    plan = HelmRuntime(settings).app_plan(
        request.model_copy(
            update={
                "migration": None,
                "resources": request.resources.model_copy(update={"database": "none"}),
            }
        ),
        ReleaseKey(value="a" * 24),
        None,
        False,
    )
    assert plan.values["strategy"]["canary"] == {"weights": [10, 50, 100], "holdSeconds": 5}


@pytest.mark.parametrize("initial,other", [("blue_green", "canary"), ("canary", "blue_green")])
def test_app_strategy_locked_after_first_apply(workflow, initial, other):
    from controlplane.providers.onprem.helm.adapter import HelmAdapter

    w = workflow
    w.request.strategy = Strategy(initial)
    finish(w, w.enqueue("strategy-base-request-123"))
    adapter = HelmAdapter(w.runtime.settings, store=w.store)
    env = adapter.public_environment()
    assert env["strategy_locked"]
    assert env["helm_strategy_options"] == [initial]
    assert set(env["helm_strategy_unavailable"]) == {other}
    assert "기존 앱의 전략 변경" in env["helm_strategy_unavailable"][other]
    assert adapter.parse_request(w.request.model_dump(mode="json")).strategy == initial
    w.request.strategy = Strategy(other)
    with pytest.raises(ValueError, match="strategy is immutable"):
        adapter.parse_request(w.request.model_dump(mode="json"))


def test_new_onprem_application_accepts_both_strategies_in_same_environment(settings, store):
    from controlplane.providers.onprem.helm.adapter import HelmAdapter

    settings.database_url = settings.database_url.__class__(store.dsn)
    adapter = HelmAdapter(settings, store=store)
    env = adapter.public_environment()
    assert env["name"] == "온프렘 · K3s / Helm"
    assert env["helm_strategy_options"] == ["blue_green", "canary"]
    assert env["helm_strategy_unavailable"] == {}
    assert not env["strategy_locked"]
    for strategy in (Strategy.BLUE_GREEN, Strategy.CANARY):
        request = settings.demo_request()
        request.strategy = strategy
        parsed = adapter.parse_request(request.model_dump(mode="json"))
        assert parsed.environment_id == env["id"]
        assert parsed.application == env["application"]
        assert parsed.strategy == strategy


def test_demo_defaults_do_not_change_existing_binding_fingerprint(settings):
    before = settings.fingerprint()
    settings.default_strategy = "canary"
    settings.demo_database = False
    assert settings.fingerprint() == before


def test_canary_rollback_waits_for_controller_before_spending_http_budget(workflow):
    w = workflow
    w.request.strategy = Strategy.CANARY
    finish(w, w.enqueue("canary-restore-base-123"))
    row = w.enqueue("canary-restore-failure-123")
    while row["state"] != "canary_waiting":
        row = w.step(row)
    w.runtime.observe = lambda *a: rollout_state(
        uid="rollout-uid", generation=2, ready=False, failed=True
    )
    row = w.step(row)
    assert row["state"] == "restore_checking"
    w.runtime.observe = lambda *a: rollout_state(
        uid="rollout-uid", generation=3, ready=False, failed=False
    )
    for _ in range(15):
        row = w.step(row)
    assert row["checkpoint"]["attempts"] == 0
    w.runtime.observe = lambda *a: rollout_state(
        uid="rollout-uid", generation=3, ready=True, failed=False
    )
    restored = finish(w, row)
    assert restored["state"] == "failed"
    assert restored["error_code"] == "release_failed_restored"


def test_canary_plan_explains_only_requested_operations(settings, store):
    from controlplane.providers.interface import DeploymentSpec
    from controlplane.providers.onprem.helm.adapter import HelmAdapter

    settings.database_url = settings.database_url.__class__(store.dsn)
    settings.demo_database = False
    request = settings.demo_request()
    request.strategy = Strategy.CANARY
    adapter = HelmAdapter(settings, store=store)
    plan = adapter.plan(DeploymentSpec.model_validate(request.model_dump()))
    steps = plan.steps
    assert [s.id for s in steps] == ["helm", "canary", "verify"]
    assert "10% → 50% → 100%" in steps[1].detail
    assert plan.blockers == []
    assert plan.mode == "plan_only" and plan.executable is False
    assert store.list() == []


@pytest.mark.parametrize("initial,other", [("blue_green", "canary"), ("canary", "blue_green")])
def test_strategy_bridge_retains_verified_version_before_new_candidate(workflow, initial, other):
    w = workflow
    w.runtime.settings.app_chart.version = "0.3.0"
    w.request.strategy = Strategy(initial)
    first = finish(w, w.enqueue("bridge-initial-12345678"))
    assert first["state"] == "succeeded"
    w.request.strategy = Strategy(other)
    second = finish(w, w.enqueue("bridge-candidate-123456"))
    assert second["state"] == "succeeded"
    bridge, candidate = w.runtime.app.applies[-2:]
    assert bridge["releaseId"] == first["id"]
    assert candidate["releaseId"] == second["id"]
    assert bridge["strategy"]["type"] == candidate["strategy"]["type"]
    assert (
        second["checkpoint"]["previous"]["receipt"]["revision"]
        == first["checkpoint"]["app_receipt"]["revision"]
    )
