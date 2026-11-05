from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from kubernetes.client.exceptions import ApiException
from test_helm_worker import settings as settings

from controlplane.domain.models import Strategy
from controlplane.providers.contracts import ProviderConflict, ProviderUnavailable, ReleaseKey
from controlplane.providers.onprem.helm.runtime import HelmRuntime


def canary_plan(settings):
    request = settings.demo_request()
    request.strategy = Strategy.CANARY
    request.resources.database = "none"
    request.migration = None
    return HelmRuntime(settings).app_plan(request, ReleaseKey(value="c" * 24), None, False)


def rollout_for(plan, **status):
    values = plan.values
    return {
        "metadata": {
            "name": plan.release,
            "namespace": plan.namespace,
            "uid": "rollout-uid",
            "resourceVersion": "7",
            "generation": 2,
            "labels": {
                "launchpad.io/application": values["application"],
                "launchpad.io/environment": values["environment"],
                "app.kubernetes.io/instance": plan.release,
                "app.kubernetes.io/managed-by": "Helm",
            },
            "annotations": {
                "meta.helm.sh/release-name": plan.release,
                "meta.helm.sh/release-namespace": plan.namespace,
            },
        },
        "spec": {
            "template": {
                "metadata": {"labels": {"launchpad.io/release": values["releaseId"]}},
                "spec": {"containers": [{"image": values["image"]}]},
            },
            "strategy": {"canary": {}},
        },
        "status": {
            "observedGeneration": 2,
            "phase": "Progressing",
            "currentStepIndex": 1,
            **status,
        },
    }


def traffic_for(plan, services, **meta):
    return {
        "metadata": {
            "name": plan.release + "-traffic",
            "namespace": plan.namespace,
            "uid": "traffic-uid",
            "annotations": {
                "meta.helm.sh/release-name": plan.release,
                "meta.helm.sh/release-namespace": plan.namespace,
            },
            **meta,
        },
        "spec": {"weighted": {"services": services}},
    }


def weighted(plan, stable, canary):
    return [
        {"name": plan.release + "-stable", "weight": stable},
        {"name": plan.release + "-canary", "weight": canary},
    ]


def runtime_with(settings, plan, rollout, traffic, pods=None):
    runtime = HelmRuntime(settings)
    runtime.clients = Mock()

    def fetch(group, version, namespace, plural, name, **kwargs):
        if plural == "rollouts":
            if rollout is None:
                raise ApiException(status=404)
            return rollout
        return traffic

    runtime.clients.custom.get_namespaced_custom_object.side_effect = fetch
    runtime.clients.core.list_namespaced_pod.return_value = NS(items=pods or [])
    return runtime


def test_reconciled_canary_weights_are_reported(settings):
    plan = canary_plan(settings)
    runtime = runtime_with(
        settings, plan, rollout_for(plan), traffic_for(plan, weighted(plan, 80, 20))
    )
    state = runtime.observe(plan)
    assert state.canary_weight == 20
    assert state.traffic_uid == "traffic-uid"
    assert state.step_index == 1


def test_unset_weights_mark_the_candidate_not_ready(settings):
    plan = canary_plan(settings)
    runtime = runtime_with(
        settings,
        plan,
        rollout_for(plan, phase="Healthy", stableRS="h", currentPodHash="h", readyReplicas=1),
        traffic_for(plan, weighted(plan, None, None)),
    )
    state = runtime.observe(plan)
    assert state.ready is False
    assert state.canary_weight is None
    assert state.traffic_uid == "traffic-uid"


@pytest.mark.parametrize(
    ("traffic_change", "message"),
    [
        (
            lambda p: traffic_for(p, weighted(p, 80, 20), name="other"),
            "Foreign canary traffic resource",
        ),
        (
            lambda p: traffic_for(p, weighted(p, 80, 20), deletionTimestamp="now"),
            "Foreign canary traffic resource",
        ),
        (
            lambda p: traffic_for(p, [{"name": "foreign", "weight": 100}]),
            "Foreign canary traffic targets",
        ),
        (lambda p: traffic_for(p, weighted(p, 50, 20)), "weights are not reconciled"),
        (
            lambda p: traffic_for(p, [{"name": p.release + "-stable", "weight": 100}]),
            "Foreign canary traffic targets",
        ),
        (lambda p: traffic_for(p, weighted(p, "80", 20)), "weights are not reconciled"),
        (lambda p: traffic_for(p, weighted(p, 120, -20)), "weights are not reconciled"),
    ],
)
def test_unreconciled_canary_traffic_is_rejected(settings, traffic_change, message):
    plan = canary_plan(settings)
    runtime = runtime_with(settings, plan, rollout_for(plan), traffic_change(plan))
    with pytest.raises(ProviderConflict, match=message):
        runtime.observe(plan)


def test_foreign_traffic_annotation_is_rejected(settings):
    plan = canary_plan(settings)
    traffic = traffic_for(plan, weighted(plan, 80, 20))
    traffic["metadata"]["annotations"]["meta.helm.sh/release-name"] = "someone-else"
    runtime = runtime_with(settings, plan, rollout_for(plan), traffic)
    with pytest.raises(ProviderConflict, match="Foreign canary traffic resource"):
        runtime.observe(plan)


def test_missing_rollout_is_none_only_without_a_known_uid(settings):
    plan = canary_plan(settings)
    runtime = runtime_with(settings, plan, None, None)
    assert runtime.observe(plan) is None
    with pytest.raises(ApiException):
        runtime.observe(plan, "rollout-uid")


def test_startup_diagnostics_never_break_observation(settings, monkeypatch):
    plan = canary_plan(settings)
    runtime = runtime_with(
        settings,
        plan,
        rollout_for(plan, currentPodHash="h"),
        traffic_for(plan, weighted(plan, 80, 20)),
    )
    runtime.clients.core.list_namespaced_pod.side_effect = ApiException(status=500)
    assert runtime.observe(plan).startup_issue is None
    runtime.clients.core.list_namespaced_pod.side_effect = ProviderUnavailable("down")
    assert runtime.observe(plan).startup_issue is None


def test_blue_green_plan_skips_the_traffic_resource(settings):
    request = settings.demo_request()
    request.resources.database = "none"
    request.migration = None
    plan = HelmRuntime(settings).app_plan(request, ReleaseKey(value="d" * 24), None, False)
    rollout = rollout_for(plan)
    rollout["spec"]["strategy"] = {"blueGreen": {}}
    runtime = runtime_with(settings, plan, rollout, None)
    state = runtime.observe(plan)
    assert state.traffic_uid is None
    assert state.canary_weight is None
