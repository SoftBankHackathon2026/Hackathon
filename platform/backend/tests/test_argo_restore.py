"""Console rollback to an earlier version on the on-prem Argo Rollouts environment."""

from test_argo import TERMINAL, advance, harness, sdk  # noqa: F401

from controlplane.domain.models import Runtime
from controlplane.providers.interface import DeploymentOperation
from controlplane.providers.onprem.adapter import OnpremAdapter


def release(h, version, key):
    request = h.request.model_copy(update={"env": {"VERSION": version}})
    row = h.store.enqueue(
        request, key, h.runtime.settings.fingerprint(), runtime=Runtime.ONPREM_ARGO
    )
    done = advance(h, row, TERMINAL)
    assert done["state"] == "succeeded", done["error_code"]
    return done


def container_env(h):
    container = h.api.rollout["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container.get("env", [])}


def test_argo_restore_rolls_the_first_template_out_again(harness):  # noqa: F811
    h = harness
    first = release(h, "v1", "argo-restore-first-0001")
    release(h, "v2", "argo-restore-second-001")
    assert container_env(h)["VERSION"] == "v2"

    adapter = OnpremAdapter(h.runtime.settings, store=h.store)
    restored = adapter.restore(
        DeploymentOperation(**{k: first[k] for k in DeploymentOperation.model_fields}),
        "argo-restore-again-0001",
    )
    row = advance(h, h.store.get(restored.id), TERMINAL)
    assert row["state"] == "succeeded", row["error_code"]
    assert container_env(h)["VERSION"] == "v1"
    assert adapter.observe(restored).restore_of == first["id"]
