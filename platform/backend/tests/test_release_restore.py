"""Console rollback to an earlier version on GCP and on-prem Helm (AWS: tests/aws,
Argo: test_argo_restore). Deploy #1, deploy #2, restore #1: a new release that runs
the provider's normal workflow with #1's request (its migration is not re-run)."""

import pytest
from test_gcp import TERMINAL, advance, harness, sdk  # noqa: F401
from test_helm_worker import finish, settings, workflow  # noqa: F401

from controlplane.core.errors import RequestConflict
from controlplane.domain.models import Runtime
from controlplane.providers.gcp.adapter import GcpAdapter
from controlplane.providers.interface import DeploymentOperation
from controlplane.providers.onprem.helm.adapter import HelmAdapter


def operation(row):
    return DeploymentOperation(**{key: row[key] for key in DeploymentOperation.model_fields})


# ------------------------------------------------------------------ GCP


def gcp_release(h, version, key):
    request = h.request.model_copy(update={"env": {"VERSION": version}})
    row = h.store.enqueue(
        request,
        key,
        h.settings.fingerprint(),
        runtime=Runtime.GCP_CLOUD_RUN,
        timeout_seconds=h.settings.release_timeout_seconds,
    )
    return gcp_finish(h, row)


def gcp_finish(h, row):
    advance(h, row, {"holding"})
    h.clock += 30
    advance(h, row, {"baking"})
    h.clock += 15
    done = advance(h, row, TERMINAL)
    assert done["state"] == "succeeded", done["error_code"]
    return done


def env_of(h):
    return {e.name: e.value for e in h.api.service.template.containers[0].env}


def test_gcp_restore_runs_the_first_request_through_the_canary_again(harness):  # noqa: F811
    h = harness
    first = gcp_release(h, "v1", "gcp-restore-first-0001")
    gcp_release(h, "v2", "gcp-restore-second-001")
    assert env_of(h)["VERSION"] == "v2"

    adapter = GcpAdapter(h.settings, store=h.store)
    restored = adapter.restore(operation(first), "gcp-restore-again-0001")
    row = gcp_finish(h, h.store.get(restored.id))
    assert env_of(h)["VERSION"] == "v1"
    assert env_of(h)["LAUNCHPAD_RELEASE_ID"] == row["id"]
    status = adapter.observe(restored)
    assert status.restore_of == first["id"] and status.status == "succeeded"
    assert adapter.public_environment()["release_restore"] is True


def test_gcp_restore_refuses_unfinished_releases(harness):  # noqa: F811
    h = harness
    row = h.store.enqueue(
        h.request,
        "gcp-restore-active-0001",
        h.settings.fingerprint(),
        runtime=Runtime.GCP_CLOUD_RUN,
    )
    with pytest.raises(RequestConflict, match="성공"):
        GcpAdapter(h.settings, store=h.store).restore(operation(row), "gcp-restore-bad-00001")


# ------------------------------------------------------------------ Helm


def test_helm_restore_reinstalls_the_first_values_without_its_migration(workflow):  # noqa: F811
    w = workflow
    w.request.env = {"VERSION": "v1"}
    w.request.migration = None
    first = finish(w, w.enqueue("helm-restore-first-0001"))
    w.request.env = {"VERSION": "v2"}
    second = finish(w, w.enqueue("helm-restore-second-001"))
    assert first["state"] == second["state"] == "succeeded"
    assert w.runtime.app.applies[-1]["env"] == {"VERSION": "v2"}

    # Pretend #1 had a migration: a restore must not run it again.
    with w.store.connect() as conn:
        conn.execute(
            """UPDATE releases SET request = jsonb_set(request, '{migration}',
               '{"argv": ["migrate"], "timeout_seconds": 60, "backward_compatible": true}')
               WHERE id=%s""",
            (first["id"],),
        )
    adapter = HelmAdapter(w.runtime.settings, store=w.store)
    restored = adapter.restore(operation(first), "helm-restore-again-0001")
    row = finish(w, w.store.get(restored.id))
    assert row["state"] == "succeeded"
    assert row["request"]["migration"] is None
    assert w.runtime.app.applies[-1]["env"] == {"VERSION": "v1"}
    assert adapter.snapshot(w.store.get(restored.id)).restore_of == first["id"]
