"""AWS deploy screen: ECS native blue/green or canary with per-request traffic-shift settings."""

import pytest

from controlplane.providers.interface import DeploymentSpec
from oneport_aws.adapter import AwsAdapter

from .test_worker import enqueue, harness, run  # noqa: F401


def test_environment_lists_strategies_with_fixed_canary_settings(harness):  # noqa: F811
    adapter = AwsAdapter(harness.settings)
    adapter.initialize()
    options = adapter.public_environment()["strategy_options"]
    env = harness.settings.environment()
    assert options == {
        "strategies": ["blue_green", "canary"],
        "canary_percent": env.canary_percent,
        "canary_bake_minutes": env.canary_bake_minutes,
        "bake_minutes": env.bake_minutes,
    }
    assert adapter.public_environment()["request_template"]["strategy"] == "blue_green"


def test_canary_request_runs_as_ecs_native_canary(harness):  # noqa: F811
    h = harness
    adapter = AwsAdapter(h.settings)
    adapter.initialize()
    request = h.make_request(strategy="canary")
    adapter.parse_request(request.model_dump(mode="json"))  # accepted at intake

    row = enqueue(h, request, "canary-release-key-01")
    run(h, row["id"], {"waiting"})
    config = h.aws.services["shop"]["deploymentConfiguration"]
    assert config["strategy"] == "CANARY"
    assert config["canaryConfiguration"] == {
        "canaryPercent": h.settings.environment().canary_percent,
        "canaryBakeTimeInMinutes": h.settings.environment().canary_bake_minutes,
    }
    h.aws.advance("shop", "SUCCESSFUL")
    assert run(h, row["id"], {"succeeded", "failed", "uncertain"})["state"] == "succeeded"


def test_rollout_settings_map_to_ecs_in_whole_minutes(harness):  # noqa: F811
    h = harness
    adapter = AwsAdapter(h.settings)
    adapter.initialize()
    rollout = {
        "canary_percent": 25,
        "hold_seconds": 90,  # -> 2 minutes
        "promotion": "automatic",
        "auto_rollback": True,
        "failure_threshold": 3,
        "bake_seconds": 61,  # -> 2 minutes
    }
    request = h.make_request(strategy="canary", rollout=rollout)
    adapter.validate(DeploymentSpec.model_validate(request.model_dump(mode="json")))
    row = enqueue(h, request, "canary-rollout-key-01")
    run(h, row["id"], {"waiting"})
    config = h.aws.services["shop"]["deploymentConfiguration"]
    assert config["bakeTimeInMinutes"] == 2
    assert config["canaryConfiguration"] == {"canaryPercent": 25.0, "canaryBakeTimeInMinutes": 2}


@pytest.mark.parametrize(
    "change, message",
    [({"promotion": "manual"}, "수동"), ({"auto_rollback": False}, "자동 복귀")],
)
def test_manual_promotion_and_disabled_rollback_are_rejected(harness, change, message):  # noqa: F811
    template = AwsAdapter(harness.settings).public_environment()["request_template"]["rollout"]
    request = harness.make_request(strategy="canary", rollout={**template, **change})
    with pytest.raises(ValueError, match=message):
        harness.runtime.provider.validate(request)


def test_template_rollout_matches_operator_defaults(harness):  # noqa: F811
    env = harness.settings.environment()
    rollout = AwsAdapter(harness.settings).public_environment()["request_template"]["rollout"]
    assert rollout["promotion"] == "automatic" and rollout["auto_rollback"] is True
    assert rollout["hold_seconds"] == env.canary_bake_minutes * 60
    assert rollout["bake_seconds"] == env.bake_minutes * 60
