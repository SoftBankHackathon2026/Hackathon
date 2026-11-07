import pytest
from fastapi.testclient import TestClient

from controlplane.domain.migration import Stage
from controlplane.domain.task_plan import (
    PlannedStep,
    Signals,
    StepDef,
    StepId,
    StepStatus,
    phase_status,
    plan_steps,
    route_label,
)
from controlplane.main import create_app

IDLE = Signals(
    source_connected=True,
    target_connected=True,
    mapping_status=None,
    stage=None,
    failed_stage=None,
    verify_passed=None,
)


def statuses(source, target, signals=IDLE):
    return {step.definition.id: step.status for step in plan_steps(source, target, signals)}


def tools(source, target):
    return {step.definition.id: step.tool for step in plan_steps(source, target, IDLE)}


def at(stage, failed=None, verify=None):
    return Signals(True, True, "ready", stage, failed, verify)


def test_aws_to_onprem_uses_the_onprem_import_tools():
    found = tools("aws", "onprem")
    assert found[StepId.IMAGES] == "migration.image_migration"
    assert found[StepId.SECRETS] == "migration.secrets"
    assert found[StepId.IDENTITY] == "migration.identity"
    assert found[StepId.INFRA_CHECK].startswith("migration.harbor_infra")
    assert found[StepId.DATA_SYNC] == "migration.cutover"


@pytest.mark.parametrize(
    "source, target", [("onprem", "aws"), ("onprem", "gcp"), ("gcp", "onprem")]
)
def test_directions_without_a_tool_are_marked_unsupported(source, target):
    found = statuses(source, target)
    assert found[StepId.IMAGES] == StepStatus.UNSUPPORTED
    assert found[StepId.IDENTITY] == StepStatus.UNSUPPORTED
    assert found[StepId.DATA_SYNC] == StepStatus.PENDING


def test_cloud_to_cloud_moves_data_and_secrets_with_the_cloud_tools():
    found = tools("aws", "gcp")
    assert found[StepId.DATA_SYNC] == "migration.cloud"
    assert found[StepId.SECRETS] == "crosscloud"
    assert found[StepId.IMAGES] is None
    assert found[StepId.INFRA_CHECK] is None


def test_platform_runs_connection_mapping_and_cutover_steps_today():
    runnable = {step.definition.id for step in plan_steps("aws", "onprem", IDLE) if step.runnable}
    assert runnable == {
        StepId.CONNECT,
        StepId.DATA_MAPPING,
        StepId.DATA_SYNC,
        StepId.TRAFFIC_SWITCH,
        StepId.VERIFY,
    }


def test_environment_steps_follow_connection_and_mapping():
    disconnected = Signals(True, False, "needs_review", None, None, None)
    assert statuses("aws", "onprem", disconnected)[StepId.CONNECT] == StepStatus.PENDING
    assert statuses("aws", "onprem", disconnected)[StepId.DATA_MAPPING] == StepStatus.PENDING
    confirmed = Signals(True, True, "ready", None, None, None)
    assert statuses("aws", "onprem", confirmed)[StepId.CONNECT] == StepStatus.DONE
    assert statuses("aws", "onprem", confirmed)[StepId.DATA_MAPPING] == StepStatus.DONE


@pytest.mark.parametrize(
    "signals, expected",
    [
        (at(Stage.COPYING), (StepStatus.RUNNING, StepStatus.PENDING, StepStatus.PENDING)),
        (at(Stage.READY), (StepStatus.WAITING, StepStatus.PENDING, StepStatus.PENDING)),
        (at(Stage.FENCING), (StepStatus.DONE, StepStatus.RUNNING, StepStatus.PENDING)),
        (at(Stage.SWITCHING), (StepStatus.DONE, StepStatus.RUNNING, StepStatus.PENDING)),
        (at(Stage.VERIFYING), (StepStatus.DONE, StepStatus.DONE, StepStatus.RUNNING)),
        (at(Stage.COMPLETED, verify=True), (StepStatus.DONE,) * 3),
        (at(Stage.COMPLETED, verify=False), (StepStatus.DONE, StepStatus.DONE, StepStatus.FAILED)),
        (
            at(Stage.ABORTED, failed=Stage.COPYING),
            (StepStatus.FAILED, StepStatus.PENDING, StepStatus.PENDING),
        ),
        (
            at(Stage.NEEDS_RECOVERY, failed=Stage.SWITCHING),
            (StepStatus.DONE, StepStatus.FAILED, StepStatus.PENDING),
        ),
        (at(Stage.ABORTED), (StepStatus.FAILED, StepStatus.FAILED, StepStatus.FAILED)),
    ],
)
def test_cutover_stages_map_onto_the_three_application_steps(signals, expected):
    found = statuses("onprem", "aws", signals)
    assert (found[StepId.DATA_SYNC], found[StepId.TRAFFIC_SWITCH], found[StepId.VERIFY]) == expected


def step(status):
    return PlannedStep(StepDef(StepId.IMAGES, None, "", ""), "tool", status, False)


@pytest.mark.parametrize(
    "members, expected",
    [
        ([StepStatus.UNSUPPORTED], StepStatus.UNSUPPORTED),
        ([StepStatus.DONE, StepStatus.UNSUPPORTED], StepStatus.DONE),
        ([StepStatus.DONE, StepStatus.PENDING], StepStatus.RUNNING),
        ([StepStatus.PENDING, StepStatus.PENDING], StepStatus.PENDING),
        ([StepStatus.DONE, StepStatus.WAITING], StepStatus.WAITING),
        ([StepStatus.RUNNING, StepStatus.FAILED], StepStatus.FAILED),
    ],
)
def test_phase_status_summarises_its_supported_steps(members, expected):
    assert phase_status([step(status) for status in members]) == expected


def test_route_label_names_both_sides():
    assert route_label("aws", "onprem") == "AWS → 온프렘"
    assert route_label("onprem", "gcp") == "온프렘 → GCP"


def test_plan_endpoint_returns_both_phases_for_a_task(monkeypatch):
    monkeypatch.delenv("ONEPORT_ACCESS_PIN", raising=False)
    client = TestClient(create_app(auth_mode="demo"))
    plan = client.get("/api/v1/migrations/return_001/plan").json()
    assert plan["route"]["label"] == "AWS → 온프렘"
    assert [phase["id"] for phase in plan["phases"]] == ["environment", "application"]
    application = {s["id"]: s for s in plan["phases"][1]["steps"]}
    assert application["images"]["tool"] == "migration.image_migration"
    assert client.get("/api/v1/migrations/nope/plan").status_code == 404
