from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from argo_fakes import ArgoApi
from kubernetes import client

from controlplane.domain.models import DeploymentRequest, Runtime
from controlplane.execution.onprem.runtime import ArgoRuntime
from controlplane.execution.onprem.settings import ArgoSettings
from controlplane.execution.onprem.worker import ArgoWorker
from controlplane.execution.reconciler import ReleaseReconciler
from controlplane.providers.contracts import ProviderConflict, ReleaseKey
from controlplane.providers.onprem import ArgoEnvironment, ArgoProvider, manifests
from controlplane.providers.onprem.preflight import permissions

IMAGE = "registry.example/app@sha256:" + "a" * 64


@pytest.fixture
def sdk():
    env = ArgoEnvironment(
        application="demo", environment_id="test", hostname="demo-a1b2.example.com"
    )
    api = ArgoApi()
    serializer = client.ApiClient()
    clients = SimpleNamespace(
        api=serializer, core=api, apps=api, batch=api, networking=api, custom=api
    )
    request = DeploymentRequest(
        application="demo",
        environment_id="test",
        cpu="0.5",
        source={"kind": "image", "image": IMAGE},
    )
    yield SimpleNamespace(env=env, api=api, provider=ArgoProvider(clients, env), request=request)
    serializer.close()


def submit(sdk, release="a" * 24, expected=None):
    sdk.provider.initialize(sdk.request)
    key = ReleaseKey(value=release)
    revision = sdk.provider.submit(sdk.request, key, expected=expected)
    sdk.api.settle(sdk.env)
    return key, revision


def test_first_activation_is_not_public_and_no_preview_ingress(sdk):
    key, revision = submit(sdk)
    observation = sdk.provider.observe(sdk.request, key, expected_uid=revision.uid)
    assert observation.ready and observation.active  # Argo initially auto-activates.
    assert not any(kind == "ingress" for kind, _ in sdk.api.objects)
    sdk.provider.publish()
    ingresses = [obj for (kind, _), obj in sdk.api.objects.items() if kind == "ingress"]
    assert len(ingresses) == 1
    assert (
        ingresses[0]["metadata"]["annotations"]["traefik.ingress.kubernetes.io/router.entrypoints"]
        == "websecure"
    )
    assert "tls" not in ingresses[0]["spec"]  # Infrastructure owns default origin certificate.
    assert sdk.api.patch_calls == 0


def test_public_hub_digest_reaches_argo_without_registry_secret(sdk):
    from controlplane.domain.models import ImageSource

    image = "docker.io/team/app@sha256:" + "c" * 64
    sdk.request = sdk.request.model_copy(update={"source": ImageSource(image=image)})
    submit(sdk)
    pod = sdk.api.rollout["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == image
    assert not pod.get("imagePullSecrets")


def test_update_pauses_and_promotes_through_argo_only(sdk):
    submit(sdk)
    old = sdk.provider.revision(sdk.api.rollout)
    key, revision = submit(sdk, "b" * 24, expected=old)
    observation = sdk.provider.observe(sdk.request, key, expected_uid=revision.uid)
    assert observation.ready and observation.paused and not observation.active
    sdk.provider.promote(sdk.request, key, expected=observation.revision)
    sdk.api.settle(sdk.env)
    sdk.provider.promote(sdk.request, key, expected=sdk.provider.revision(sdk.api.rollout))
    assert sdk.api.promotions == 1
    assert sdk.api.patch_calls == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.api.rollout["status"].update(observedGeneration="0"),
        lambda s: s.api.rollout["status"].update(observedGeneration="not-a-number"),
        lambda s: s.api.replica_sets[0]["status"].update(availableReplicas=0),
        lambda s: s.api.replica_sets[0]["metadata"].update(
            ownerReferences=[{"uid": "foreign", "controller": True}]
        ),
        lambda s: s.api.replica_sets[0]["spec"]["template"]["spec"]["containers"][0].update(
            image="foreign"
        ),
        lambda s: s.api.objects[("service", manifests.name(s.env) + "-preview")]["spec"][
            "selector"
        ].update({manifests.HASH: "wrong"}),
    ],
)
def test_unobserved_or_wrong_candidate_never_ready(sdk, mutate):
    key, revision = submit(sdk)
    mutate(sdk)
    assert not sdk.provider.observe(sdk.request, key, expected_uid=revision.uid).ready


def test_generation_and_uid_guards(sdk):
    key, revision = submit(sdk)
    sdk.api.rollout["metadata"]["uid"] = "replacement"
    with pytest.raises(ProviderConflict):
        sdk.provider.observe(sdk.request, key, expected_uid=revision.uid)
    with pytest.raises(ProviderConflict):
        sdk.provider.submit(sdk.request, key, expected=revision)


def test_stale_update_and_foreign_service_fail_closed(sdk):
    _, revision = submit(sdk)
    sdk.api.rollout["metadata"]["resourceVersion"] = "2"
    sdk.api.rollout["metadata"]["generation"] = 2
    with pytest.raises(ProviderConflict):
        sdk.provider.submit(sdk.request, ReleaseKey(value="b" * 24), expected=revision)
    sdk.api.objects[("service", manifests.name(sdk.env) + "-active")]["spec"]["selector"][
        "foreign"
    ] = "pod"
    with pytest.raises(ProviderConflict):
        sdk.provider.initialize(sdk.request)


def test_controller_status_progress_does_not_invalidate_prepared_spec(sdk):
    _, revision = submit(sdk)
    sdk.api.rollout["metadata"]["resourceVersion"] = "9"
    result = sdk.provider.submit(sdk.request, ReleaseKey(value="b" * 24), expected=revision)
    assert result.generation == 2


@pytest.mark.parametrize(
    "policy", ["prePromotionAnalysis", "autoPromotionSeconds", "postPromotionAnalysis"]
)
def test_injected_promotion_policy_is_rejected(sdk, policy):
    key, revision = submit(sdk)
    sdk.api.rollout["spec"]["strategy"]["blueGreen"][policy] = {"unexpected": True}
    with pytest.raises(ProviderConflict):
        sdk.provider.observe(sdk.request, key, expected_uid=revision.uid)


def test_real_sdk_requests_use_supported_replace_and_status_patch_methods(sdk):
    submit(sdk)
    real_api = client.ApiClient()
    real_api.call_api = Mock(side_effect=lambda *args, **kwargs: deepcopy(sdk.api.rollout))
    sdk.provider.clients.custom = client.CustomObjectsApi(real_api)
    expected = sdk.provider.revision(sdk.api.rollout)
    # Actual generated SDK serializes PUT; unsupported _content_type kwargs would raise.
    desired = manifests.rollout(sdk.request, sdk.env, ReleaseKey(value="b" * 24))
    response = deepcopy(desired)
    response["metadata"].update(uid=expected.uid, resourceVersion="2", generation=2)
    real_api.call_api.side_effect = [deepcopy(sdk.api.rollout), response]
    sdk.provider.submit(sdk.request, ReleaseKey(value="b" * 24), expected=expected)
    args = real_api.call_api.call_args.args
    assert args[1] == "PUT"
    assert (
        real_api.call_api.call_args.kwargs["body"]["metadata"]["resourceVersion"]
        == expected.resource_version
    )
    real_api.close()


def test_permissions_identify_missing_infra_grants_and_do_not_require_service_patch(sdk):
    request = sdk.request.model_copy(update={"migration": None})
    rules = permissions(sdk.provider, request)
    assert not any(r["resource"] == "jobs" for r in rules)
    assert {
        r["verb"]
        for r in permissions(sdk.provider, request, preview_job=True)
        if r["resource"] == "jobs"
    } == {"get", "create"}
    assert any(r["resource"] == "serviceaccounts" and r["name"] == "oneport-runtime" for r in rules)
    assert not any(r["resource"] == "services" and r["verb"] == "patch" for r in rules)
    request = DeploymentRequest.model_validate(
        {
            **request.model_dump(),
            "resources": {"database": "postgresql"},
            "migration": {"argv": ["alembic", "upgrade", "head"], "backward_compatible": True},
        }
    )
    assert {r["verb"] for r in permissions(sdk.provider, request) if r["resource"] == "jobs"} == {
        "get",
        "create",
    }


@pytest.fixture
def harness(store, sdk):
    settings = ArgoSettings(
        environment=sdk.env,
        kubeconfig=Path("/operator/config"),
        context="oneport",
        server="https://example.com:6443",
        database_url=store.dsn,
        allowed_images=[IMAGE],
        production_check_limit=3,
    )
    sdk.store = store
    sdk.runtime = SimpleNamespace(
        settings=settings, provider=sdk.provider, connect=Mock(), probe=Mock(return_value=True)
    )
    return sdk


def enqueue(h, key="argo-test-key-0001"):
    return h.store.enqueue(
        h.request, key, h.runtime.settings.fingerprint(), runtime=Runtime.ONPREM_ARGO
    )


def tick(h):
    with h.store.connect() as conn:
        conn.execute("UPDATE releases SET next_attempt_at=now()")
    h.api.settle(h.env)
    ArgoWorker(h.store, h.runtime).tick()


def advance(h, row, states, limit=60):
    for _ in range(limit):
        result = h.store.get(row["id"])
        if result["state"] in states:
            return result
        tick(h)
    raise AssertionError(h.store.get(row["id"])["state"])


TERMINAL = {"succeeded", "failed", "uncertain"}


def test_worker_full_first_deploy_then_update_and_provider_queue_isolation(harness):
    h = harness
    row = enqueue(h)

    class OtherWorker(ReleaseReconciler):
        queue_runtime = Runtime.AWS_ECS

    OtherWorker(h.store, h.runtime).tick()
    h.runtime.connect.assert_not_called()
    advance(h, row, {"promoting"})
    assert not any(kind == "ingress" for kind, _ in h.api.objects)
    assert advance(h, row, TERMINAL)["state"] == "succeeded"
    assert h.api.promotions == 0
    row = enqueue(h, "argo-test-key-0002")
    assert advance(h, row, TERMINAL)["state"] == "succeeded"
    assert h.api.promotions == 1 and h.api.patch_calls == 0


def test_crash_after_submit_reuses_rollout(harness):
    h = harness
    row = enqueue(h)
    advance(h, row, {"submitting"})
    original = h.provider.submit

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit()

    h.provider.submit = crash
    with pytest.raises(SystemExit):
        tick(h)
    creates = h.api.creates
    h.provider.submit = original
    assert advance(h, row, TERMINAL)["state"] == "succeeded"
    assert h.api.creates == creates + 1  # Only the late public Ingress is created.


def test_crash_after_promotion_does_not_patch_service_or_promote_again(harness):
    h = harness
    first = enqueue(h)
    assert advance(h, first, TERMINAL)["state"] == "succeeded"
    row = enqueue(h, "argo-test-key-0002")
    advance(h, row, {"promoting"})
    original = h.provider.promote

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit()

    h.provider.promote = crash
    with pytest.raises(SystemExit):
        tick(h)
    h.provider.promote = original
    assert advance(h, row, TERMINAL)["state"] == "succeeded"
    assert h.api.promotions == 1 and h.api.patch_calls == 0


@pytest.mark.parametrize("fail_private", [False, True])
def test_failed_update_restores_saved_template_and_rechecks(harness, fail_private):
    h = harness
    first = enqueue(h)
    assert advance(h, first, TERMINAL)["state"] == "succeeded"
    row = enqueue(h, "argo-test-key-0002")
    h.runtime.probe.side_effect = lambda req, key, *, production: (
        key.value != row["id"] or (not production and not fail_private)
    )
    result = advance(h, row, TERMINAL)
    assert result["state"] == "failed" and result["error_code"] == "release_failed_restored"
    assert h.provider.revision(h.api.rollout).release == first["id"]
    assert h.api.patch_calls == 0


def test_first_failed_production_unpublishes_and_blocks_blind_redeploy(harness):
    h = harness
    row = enqueue(h)
    h.runtime.probe.side_effect = lambda req, key, *, production: not production
    assert advance(h, row, TERMINAL)["state"] == "uncertain"
    assert not any(kind == "ingress" for kind, _ in h.api.objects)
    assert h.api.rollout is not None  # Retain evidence; no arbitrary cascade deletion.


def test_private_probe_uses_cluster_dns_not_public_preview(harness):
    h = harness
    rt = ArgoRuntime(h.runtime.settings)
    calls = []

    def respond(req):
        calls.append(str(req.url))
        return httpx.Response(200, headers={"X-Launchpad-Release": "a" * 24})

    rt.http = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        assert rt.probe(h.request, ReleaseKey(value="a" * 24), production=False)
        assert calls == [
            f"http://{manifests.name(h.env)}-preview.oneport-apps.svc.cluster.local/health"
        ]
    finally:
        rt.close()


def migration_request(h):
    from controlplane.providers.onprem.bindings import SecretRef

    h.env.secret_refs["db"] = SecretRef(name="db-v1", key="url", uid="db-version-1")
    h.provider.environment.secret_refs["db"] = h.env.secret_refs["db"]
    h.runtime.settings.environment.secret_refs["db"] = h.env.secret_refs["db"]
    h.request = DeploymentRequest.model_validate(
        {
            **h.request.model_dump(),
            "resources": {"database": "postgresql"},
            "secret_bindings": [{"name": "DATABASE_URL", "binding": "db"}],
            "migration": {"argv": ["alembic", "upgrade", "head"], "backward_compatible": True},
        }
    )


@pytest.mark.parametrize("success", [True, False])
def test_migration_gates_rollout_creation(harness, success):
    h = harness
    migration_request(h)
    row = enqueue(h)
    advance(h, row, {"migration_waiting"})
    assert h.api.rollout is None
    job = next(obj for (kind, _), obj in h.api.objects.items() if kind == "job")
    job["status"] = {
        "conditions": [{"type": "Complete" if success else "Failed", "status": "True"}],
        "succeeded": int(success),
    }
    result = advance(h, row, TERMINAL)
    assert result["state"] == ("succeeded" if success else "failed")
    assert bool(h.api.rollout) is success


def test_migration_missing_after_lost_response_is_not_replayed(harness):
    h = harness
    migration_request(h)
    row = enqueue(h)
    advance(h, row, {"initialized"})
    original = h.provider.submit_migration

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit()

    h.provider.submit_migration = crash
    with pytest.raises(SystemExit):
        tick(h)
    h.api.objects = {key: obj for key, obj in h.api.objects.items() if key[0] != "job"}
    creates = h.api.creates
    h.provider.submit_migration = original
    result = advance(h, row, TERMINAL)
    assert result["state"] == "uncertain"
    assert result["error_code"] == "migration_result_unknown"
    assert h.api.creates == creates and h.api.rollout is None


def test_plain_env_is_preserved_in_application_and_migration_without_shadowing_secrets(sdk):
    from controlplane.providers.onprem import pods
    from controlplane.providers.onprem.models import ArgoDeploymentRequest

    request = ArgoDeploymentRequest.model_validate(
        {
            **sdk.request.model_dump(),
            "env": {"APP_MODE": "test"},
            "resources": {"database": "postgresql"},
            "secret_bindings": [{"name": "DATABASE_URL", "binding": "db"}],
            "migration": {"argv": ["alembic", "upgrade", "head"], "backward_compatible": True},
        }
    )
    from controlplane.providers.onprem.bindings import SecretRef

    sdk.env.secret_refs["db"] = SecretRef(name="db-v1", key="url")
    key = ReleaseKey(value="a" * 24)
    for obj in [
        manifests.rollout(request, sdk.env, key),
        pods.migration_job(request, sdk.env, key),
    ]:
        values = obj["spec"]["template"]["spec"]["containers"][0]["env"]
        assert {"name": "APP_MODE", "value": "test"} in values
    for variable in ["PORT", "LAUNCHPAD_RELEASE_ID", "DATABASE_URL", "invalid-name"]:
        with pytest.raises(ValueError):
            ArgoDeploymentRequest.model_validate({**request.model_dump(), "env": {variable: "bad"}})
