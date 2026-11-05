"""App deletion on GCP Cloud Run and on-prem Helm (AWS: tests/aws, Argo: test_argo_deletion)."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from kubernetes.client.exceptions import ApiException
from psycopg.types.json import Jsonb
from test_gcp import harness as gcp  # noqa: F401
from test_gcp import sdk  # noqa: F401
from test_helm_worker import finish, settings, workflow  # noqa: F401

from controlplane.core.errors import RequestConflict
from controlplane.domain.models import Runtime
from controlplane.providers.gcp.adapter import GcpAdapter
from controlplane.providers.interface import DeploymentSpec
from controlplane.providers.registry import ProviderRegistry
from controlplane.repositories.deletions import DeletionStore
from controlplane.services.deployments import DeploymentService


def request_deletion(store, env_id, runtime, application, key, *, delete_data=False):
    return DeletionStore(store.dsn).request(
        application=application,
        environment_id=env_id,
        runtime=runtime,
        delete_data=delete_data,
        key=key,
    )


def run_deletion(store, env_id, tick_once, limit=30):
    deletions = DeletionStore(store.dsn)
    for _ in range(limit):
        with store.connect() as conn:
            conn.execute("UPDATE app_deletions SET next_attempt_at=now()")
        tick_once()
        row = deletions.latest(env_id)
        if row["state"] in {"deleted", "failed"}:
            return row
    raise AssertionError(f"stuck in {row['state']}")


# ------------------------------------------------------------------ GCP


def test_gcp_deletes_the_registered_service_then_refuses_new_deploys(gcp):  # noqa: F811
    h = gcp
    request_deletion(h.store, "gcp-test", Runtime.GCP_CLOUD_RUN, "demo", "gcp-delete-key-000001")
    row = run_deletion(h.store, "gcp-test", h.worker.tick)
    assert row["state"] == "deleted", row["events"]
    assert h.api.writes[-1] == (["delete"], None)
    assert [e["state"] for e in row["events"]][:2] == ["requested", "service_deleting"]

    registry = ProviderRegistry()
    adapter = GcpAdapter(h.settings, store=h.store)
    registry.register(adapter)
    assert adapter.deletion_info()["recreatable"] is False
    service = DeploymentService(registry, h.store)
    spec = DeploymentSpec.model_validate(h.request.model_dump(mode="json"))
    with pytest.raises(RequestConflict, match="삭제됐습니다"):
        service.submit(spec, "gcp-after-delete-key-01")


def test_gcp_never_deletes_a_service_it_did_not_register(gcp):  # noqa: F811
    h = gcp
    h.api.service.uid = "someone-else"
    request_deletion(h.store, "gcp-test", Runtime.GCP_CLOUD_RUN, "demo", "gcp-foreign-key-00001")
    row = run_deletion(h.store, "gcp-test", h.worker.tick)
    assert row["state"] == "failed" and "UID" in row["events"][-1]["message"]
    assert not getattr(h.api, "deleted", 0)


# ------------------------------------------------------------------ Helm


class Released:
    """Helm release fake that also uninstalls."""

    def __init__(self, helm):
        self.helm = helm
        self.uninstalls = []

    def uninstall(self, release):
        self.uninstalls.append(release)
        self.helm.revision = 0
        self.helm.history.clear()


def helm_world(w, *, data_objects=()):
    helm = w.runtime.app
    helm.uninstall = Released(helm).uninstall
    data_release = NS(revision=1 if data_objects else 0, uninstalls=[])
    w.runtime.data = NS(
        current=lambda name: (
            {"revision": 1, "status": "deployed"} if data_release.revision else None
        ),
        uninstall=lambda name: (
            data_release.revision
            and (data_release.uninstalls.append(name), setattr(data_release, "revision", 0))
        ),
    )
    objects = set(data_objects)

    def reader(kind):
        def read(name, namespace, **kwargs):
            if (kind, name) not in objects:
                raise ApiException(status=404)
            return NS(metadata=NS(uid="x"))

        return read

    def deleter(kind):
        def delete(name, namespace, **kwargs):
            objects.discard((kind, name))

        return delete

    w.runtime.clients = NS(
        core=NS(
            read_namespaced_persistent_volume_claim=reader("pvc"),
            delete_namespaced_persistent_volume_claim=deleter("pvc"),
            read_namespaced_secret=reader("secret"),
            delete_namespaced_secret=deleter("secret"),
        ),
        batch=NS(delete_namespaced_job=Mock()),
    )
    return data_release, objects


def test_helm_delete_keeps_data_then_reinstalls_from_scratch(workflow):  # noqa: F811
    w = workflow
    assert finish(w, w.enqueue("helm-before-delete-0001"))["state"] == "succeeded"
    data_release, _ = helm_world(w, data_objects={("pvc", "keep")})
    request_deletion(w.store, "helm-dev", Runtime.ONPREM_HELM, "helm-demo", "helm-delete-key-00001")
    row = run_deletion(w.store, "helm-dev", w.worker.tick)
    assert row["state"] == "deleted", row["events"]
    assert w.runtime.app.revision == 0 and data_release.uninstalls == []
    assert w.store.list() == []  # archived history
    # A fresh first install: no previous revision is expected after deletion.
    assert finish(w, w.enqueue("helm-after-delete-00001"))["state"] == "succeeded"


def test_helm_catalog_binding_takes_data_and_detaches_from_the_app(workflow):  # noqa: F811
    w = workflow
    w.runtime.settings.catalog_dynamic = True
    with w.store.connect() as conn:
        conn.execute("INSERT INTO applications(id,name) VALUES('app-1','helm-demo')")
        conn.execute(
            "INSERT INTO deployment_targets(id,name,runtime,provider_binding_id) VALUES('t-helm','온프렘','onprem_helm','onprem-helm')"
        )
        conn.execute(
            "INSERT INTO app_environments(id,application_id,target_id,runtime_application,binding) VALUES('helm-dev','app-1','t-helm','helm-demo',%s)",
            (Jsonb({}),),
        )
    from controlplane.providers.onprem.helm.plan import release_name

    data = release_name("helm-demo", "helm-dev", data=True)
    data_release, objects = helm_world(
        w, data_objects={("pvc", "data-" + data + "-postgres-0"), ("secret", data + "-credentials")}
    )
    request_deletion(
        w.store,
        "helm-dev",
        Runtime.ONPREM_HELM,
        "helm-demo",
        "helm-catalog-key-0001",
        delete_data=True,
    )
    row = run_deletion(w.store, "helm-dev", w.worker.tick)
    assert row["state"] == "deleted", row["events"]
    assert data_release.uninstalls == [data] and not objects
    with w.store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM app_environments").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM applications").fetchone()["n"] == 0
    assert "앱 목록에서도" in row["events"][-1]["message"]
