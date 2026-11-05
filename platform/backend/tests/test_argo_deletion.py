"""App deletion on the on-prem Argo Rollouts environment."""

from test_app_deletion import request_deletion, run_deletion
from test_argo import TERMINAL, advance, enqueue, harness, sdk  # noqa: F401

from controlplane.domain.models import Runtime
from controlplane.execution.onprem.worker import ArgoWorker
from controlplane.providers.onprem import manifests


def test_argo_deletes_ingress_rollout_and_services_then_redeploys(harness):  # noqa: F811
    h = harness
    assert advance(h, enqueue(h), TERMINAL)["state"] == "succeeded"
    assert ("ingress", manifests.name(h.env)) in h.api.objects
    request_deletion(h.store, "test", Runtime.ONPREM_ARGO, "demo", "argo-delete-key-00001")
    row = run_deletion(h.store, "test", lambda: ArgoWorker(h.store, h.runtime).tick())
    assert row["state"] == "deleted", row["events"]
    assert h.api.rollout is None
    assert not any(kind in {"ingress", "service"} for kind, _ in h.api.objects)
    assert advance(h, enqueue(h, "argo-after-delete-0001"), TERMINAL)["state"] == "succeeded"
