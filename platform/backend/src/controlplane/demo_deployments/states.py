from collections.abc import Iterable
from enum import StrEnum


class RunState(StrEnum):
    QUEUED = "queued"
    BUILDING = "building"
    DEPLOYING = "deploying"
    UNCERTAIN = "uncertain"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"


class TargetState(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    SKIPPED = "skipped"


class TargetBlocker(StrEnum):
    NOT_CONFIGURED = "target_not_configured"
    IMAGE_DEPLOY_UNSUPPORTED = "target_image_deploy_unsupported"
    DATA_FAILED = "target_data_failed"
    DATA_PREPARING = "target_data_preparing"
    RELEASE_IN_PROGRESS = "target_release_in_progress"
    DEFAULTS_REJECTED = "deployment_defaults_rejected"


ACTIVE_RUN_STATES = (
    RunState.QUEUED,
    RunState.BUILDING,
    RunState.DEPLOYING,
    RunState.UNCERTAIN,
)
ACTIVE_VALUES = [state.value for state in ACTIVE_RUN_STATES]
SETTLED_TARGET_STATES = frozenset({TargetState.SUCCEEDED, TargetState.FAILED, TargetState.SKIPPED})
IN_FLIGHT_TARGET_STATES = frozenset({TargetState.PENDING, TargetState.RUNNING, TargetState.QUEUED})
RELEASE_STATES_KEPT = frozenset(
    {
        TargetState.QUEUED,
        TargetState.SUCCEEDED,
        TargetState.FAILED,
        TargetState.UNCERTAIN,
    }
)
RELEASE_SETTLED = ("succeeded", "failed")


def aggregate_run_state(states: Iterable[str]) -> RunState:
    states = list(states)
    if any(state in IN_FLIGHT_TARGET_STATES for state in states):
        return RunState.DEPLOYING
    if TargetState.UNCERTAIN in states:
        return RunState.UNCERTAIN
    if all(state == TargetState.SUCCEEDED for state in states):
        return RunState.SUCCEEDED
    return RunState.PARTIAL if TargetState.SUCCEEDED in states else RunState.FAILED
