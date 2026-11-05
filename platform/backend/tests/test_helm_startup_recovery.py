"""First-deploy recovery uses the recorded private revision and retains its data."""

from copy import deepcopy
from types import SimpleNamespace as NS

import pytest
from helm_fakes import rollout_state
from test_helm_worker import FakeHelm, finish
from test_helm_worker import clients as clients
from test_helm_worker import settings as settings
from test_helm_worker import workflow as workflow

from controlplane.providers.contracts import ProviderConflict
from controlplane.providers.onprem.helm import data
from controlplane.providers.onprem.helm.observation import observe, startup_issue


def waiting(w, row):
    for _ in range(20):
        if row["state"] == "app_waiting":
            return row
        row = w.step(row)
    pytest.fail("candidate was not installed")


def fail_candidate(w, row):
    healthy = w.runtime.observe
    w.runtime.observe = lambda *a: rollout_state(
        uid="rollout-uid",
        generation=1,
        ready=False,
        failed=False,
        startup_issue="앱이 반복 재시작 중입니다. DATABASE_URL 설정을 확인하세요.",
    )
    first = w.step(row)
    assert first["state"] == "app_waiting"
    assert "반복 재시작" in w.store.get(row["id"])["events"][-1]["message"]
    failed = finish(w, first)
    assert failed["state"] == "failed"
    assert failed["error_code"] == "app_startup_failed"
    w.runtime.observe = healthy
    return failed


@pytest.mark.parametrize("database_first", [False, True])
def test_failed_first_deploy_can_add_or_reuse_database(
    workflow, clients, monkeypatch, database_first
):
    w = workflow
    w.runtime.clients = clients
    w.runtime.data = FakeHelm()
    monkeypatch.setattr(data, "observe", lambda *a: (True, {"StatefulSet/postgres": "pg-uid"}))
    if database_first:
        w.request.resources.database = "postgresql"
    failed = fail_candidate(w, waiting(w, w.enqueue("initial-private-candidate")))
    prior_data = failed["checkpoint"].get("data_values")
    original_secret = (
        deepcopy(clients.core.read_namespaced_secret.return_value.data) if database_first else None
    )
    w.request.resources.database = "postgresql"
    recovered = finish(w, w.enqueue("corrected-database-request"))
    assert recovered["state"] == "succeeded"
    assert recovered["checkpoint"]["previous"] is None
    assert recovered["checkpoint"]["retry_base"]["receipt"]["revision"] == 1
    assert [p["route"]["enabled"] for p in w.runtime.app.applies] == [False, False, True]
    assert len({p["application"] for p in w.runtime.app.applies}) == 1
    assert len(w.runtime.data.applies) == 1
    assert clients.core.create_namespaced_secret.call_count == 1
    assert (
        recovered["checkpoint"]["app_plan"]["values"]["secretBindings"][0]["env"] == "DATABASE_URL"
    )
    if database_first:
        assert recovered["checkpoint"]["data_values"] == prior_data
        assert clients.core.read_namespaced_secret.return_value.data == original_secret
        assert recovered["checkpoint"]["data_uids"] == failed["checkpoint"]["data_uids"]


@pytest.mark.parametrize("mutation", ["revision", "values", "uid"])
def test_retry_refuses_changed_candidate(workflow, mutation):
    w = workflow
    fail_candidate(w, waiting(w, w.enqueue("initial-private-candidate")))
    if mutation == "revision":
        w.runtime.app.revision += 1
    elif mutation == "values":
        w.runtime.app.history[1].values["port"] = 9999
    else:

        def replaced(*args):
            raise ProviderConflict("Helm Rollout was replaced")

        w.runtime.observe = replaced
    with pytest.raises(ProviderConflict):
        w.step(w.enqueue("corrected-private-candidate"))
    assert len(w.runtime.app.applies) == 1


@pytest.mark.parametrize("changed", [False, True])
def test_old_uncertain_first_failure_is_reobserved_without_cloud_writes(workflow, changed):
    w = workflow
    row = waiting(w, w.enqueue("old-private-candidate"))
    row = w.step(row)  # Persist the rollout UID/generation.
    with w.store.worker_lock() as conn:
        w.store.transition(conn, row, "uncertain", "old failure", error_code="candidate_failed")
    before = len(w.runtime.app.applies)
    w.runtime.observe = lambda *a: rollout_state(
        uid="rollout-uid", generation=1, ready=False, failed=True
    )
    if changed:
        w.runtime.app.history[1].values["port"] = 9999
    w.worker._reconcile_first_failure()
    result = w.store.get(row["id"])
    assert result["state"] == ("uncertain" if changed else "failed")
    assert len(w.runtime.app.applies) == before


def test_transient_startup_issue_clears_without_failure(workflow):
    w = workflow
    row = waiting(w, w.enqueue("transient-startup-request"))
    healthy = w.runtime.observe
    w.runtime.observe = lambda *a: rollout_state(
        uid="rollout-uid",
        generation=1,
        ready=False,
        failed=False,
        startup_issue="앱이 반복 재시작 중입니다.",
    )
    row = w.step(row)
    w.runtime.observe = healthy
    assert finish(w, row)["state"] == "succeeded"


@pytest.mark.parametrize("observed", [True, False])
def test_progress_deadline_failure_is_scoped_to_observed_generation(workflow, observed):
    from test_helm_sdk import rollout

    w = workflow
    waiting(w, w.enqueue("progress-deadline-request"))
    plan = w.runtime.app.history[1]
    obj = rollout(plan)
    obj["status"].update(
        phase="Progressing",
        observedGeneration="2" if observed else "1",
        conditions=[
            {
                "type": "Progressing",
                "status": "False",
                "reason": "ProgressDeadlineExceeded",
            }
        ],
    )
    assert observe(obj, plan).failed is observed


@pytest.mark.parametrize(
    "restarts,owned,ready,issue",
    [
        (1, True, False, False),
        (3, True, False, True),
        (5, False, False, False),
        (5, True, True, False),
    ],
)
def test_startup_checks_only_owned_unready_candidate(
    workflow, clients, restarts, owned, ready, issue
):
    w = workflow
    waiting(w, w.enqueue("pod-startup-request"))
    plan = w.runtime.app.history[1]
    clients.core.list_namespaced_pod.return_value = NS(
        items=[
            NS(
                metadata=NS(
                    deletion_timestamp=None,
                    owner_references=[
                        NS(controller=True, kind="ReplicaSet", name="rs", uid="rs-uid")
                    ],
                ),
                status=NS(
                    container_statuses=[
                        NS(
                            name="app",
                            ready=ready,
                            restart_count=restarts,
                            state=NS(waiting=NS(reason="CrashLoopBackOff")),
                        )
                    ]
                ),
            )
        ]
    )
    clients.apps.read_namespaced_replica_set.return_value = NS(
        metadata=NS(
            uid="rs-uid",
            owner_references=[
                NS(controller=True, kind="Rollout", uid="rollout-uid" if owned else "foreign")
            ],
        )
    )
    result = startup_issue(clients, plan, "rollout-uid", "candidate-hash")
    assert bool(result) is issue
    if issue:
        assert "DATABASE_URL이 없습니다" in result
    selector = clients.core.list_namespaced_pod.call_args.kwargs["label_selector"]
    assert "launchpad.io/release=" + plan.values["releaseId"] in selector
    assert "rollouts-pod-template-hash=candidate-hash" in selector
