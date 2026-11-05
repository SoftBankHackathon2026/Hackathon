"""Same SDK contract against three real adapters and journal; cloud transports are doubles."""

from types import SimpleNamespace

import httpx
import pytest
from test_argo import TERMINAL, advance
from test_argo import harness as onprem_harness  # noqa: F401
from test_argo import sdk as sdk  # noqa: F401
from test_gcp_bridge import REMOTE, REPO, result
from test_gcp_bridge import settings as gcp_settings  # noqa: F401

from controlplane.core.errors import OperationNotFound, RequestConflict, UnsupportedOperation
from controlplane.execution.gcp_bridge.worker import GcpRuntime, GcpWorker
from controlplane.providers.gcp_bridge.adapter import GcpAdapter
from controlplane.providers.interface import (
    DeploymentOperation,
    DeploymentProvider,
    DeploymentSpec,
    DeploymentStatus,
)
from controlplane.providers.onprem.adapter import OnpremAdapter
from controlplane.providers.registry import ProviderRegistry
from controlplane.services.deployments import DeploymentService
from oneport_aws.adapter import AwsAdapter

from .test_worker import harness as aws_harness  # noqa: F401
from .test_worker import run


@pytest.fixture(params=["aws", "gcp", "onprem"])
def binding(request, store):
    def close():
        pass

    if request.param == "aws":
        h = request.getfixturevalue("aws_harness")
        provider = AwsAdapter(h.settings, store=store)
        payload = h.make_request().model_dump(mode="json")

        def finish(operation):
            run(h, operation.id, {"waiting"})
            h.aws.advance("shop", "SUCCESSFUL")
            run(h, operation.id, {"succeeded", "failed", "uncertain"})
    elif request.param == "onprem":
        h = request.getfixturevalue("onprem_harness")
        provider = OnpremAdapter(h.runtime.settings, store=store)
        payload = h.request.model_dump(mode="json")

        def finish(operation):
            advance(h, {"id": operation.id}, TERMINAL)
    else:
        settings = request.getfixturevalue("gcp_settings")
        provider = GcpAdapter(settings, store=store)
        payload = {
            "application": "demo",
            "environment_id": "gcp-demo",
            "source": {"kind": "repository", "repository": REPO},
        }

        def upstream(req):
            return (
                httpx.Response(202, json={"deployment_id": REMOTE})
                if req.method == "POST"
                else httpx.Response(200, json=result())
            )

        runtime = GcpRuntime(settings, transport=httpx.MockTransport(upstream))

        def finish(operation):
            worker = GcpWorker(store, runtime)
            worker.tick()
            worker.tick()

        close = runtime.close
    provider.initialize()
    yield SimpleNamespace(
        provider=provider, spec=DeploymentSpec.model_validate(payload), finish=finish, store=store
    )
    close()


def test_validate_plan_submit_observe_same_contract(binding):
    p, spec = binding.provider, binding.spec
    assert isinstance(p, DeploymentProvider)
    assert p.validate(spec) is None
    plan = p.plan(spec)
    assert not plan.executable and plan.steps
    assert binding.store.list() == []  # Planning must not enqueue or contact cloud.
    operation = p.submit(spec, "common-provider-operation-001")
    assert type(operation) is DeploymentOperation
    assert p.submit(spec, "common-provider-operation-001") == operation
    status = p.observe(operation)
    assert type(status) is DeploymentStatus and status.status == "queued"
    binding.finish(operation)
    status = p.observe(operation)
    assert type(status) is DeploymentStatus and status.status == "succeeded"
    assert status.service_url and status.image and status.events
    assert "checkpoint" not in status.model_dump()


def test_cross_environment_handle_cannot_read_release(binding):
    p = binding.provider
    operation = p.submit(binding.spec, "common-provider-operation-002")
    with pytest.raises(OperationNotFound):
        p.observe(operation.model_copy(update={"environment_id": "another-environment"}))
    with pytest.raises(OperationNotFound):
        p.observe(operation.model_copy(update={"id": "0" * 24}))


def test_common_service_routes_without_provider_branches(binding):
    registry = ProviderRegistry()
    registry.register(binding.provider)
    service = DeploymentService(registry, binding.store)
    status = service.submit(binding.spec, "common-provider-operation-003")
    assert service.observe(status.id).id == status.id
    assert service.list_releases()[0].id == status.id
    with pytest.raises(RequestConflict):
        service.submit(binding.spec, "common-provider-operation-004")
    with pytest.raises(UnsupportedOperation):
        service.request_action(status.id, "promote")


def test_unsupported_source_fails_before_enqueue(binding):
    source = {"kind": "git", "repository": REPO, "commit": "c" * 40}
    changed = DeploymentSpec.model_validate(binding.spec.requested() | {"source": source})
    with pytest.raises(UnsupportedOperation):
        binding.provider.submit(changed, "common-provider-operation-005")
    assert binding.store.list() == []


def test_gcp_rejects_environment_changes_and_keeps_omission(gcp_settings, store):  # noqa: F811
    p = GcpAdapter(gcp_settings, store=store)
    body = {
        "application": "demo",
        "environment_id": "gcp-demo",
        "source": {"kind": "repository", "repository": REPO},
    }
    spec = DeploymentSpec.model_validate(body)
    assert spec.port is None and spec.resources is None and spec.requested() == body
    for extra in [
        {"env": {"APP_COLOR": "blue"}},
        {"resources": {"database": "postgresql"}},
        {"replicas": 1},
        {"rollout": {"promotion": "manual"}},
    ]:
        with pytest.raises(UnsupportedOperation):
            p.submit(DeploymentSpec.model_validate(body | extra), "unsupported-gcp-operation-001")
    assert store.list() == []
    # Repository bridge now supports the verified Blue/Green strategy, but still
    # rejects environment changes and native configurable-rollout options.
    p.validate(DeploymentSpec.model_validate(body | {"strategy": "blue_green"}))
    with pytest.raises(ValueError):
        p.validate(DeploymentSpec.model_validate(body | {"strategy": "canary"}))


def test_sdk_source_model_serialization_preserves_discriminator():
    from controlplane.domain.models import ImageSource

    spec = DeploymentSpec(
        application="demo",
        environment_id="onprem-demo",
        source=ImageSource(image="registry.example/demo@sha256:" + "a" * 64),
    )
    assert spec.requested()["source"]["kind"] == "image"
    assert DeploymentSpec.model_validate(spec.requested()).source == spec.source
