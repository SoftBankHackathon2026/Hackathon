from enum import StrEnum

from controlplane.domain.models import Runtime


class ReleaseState(StrEnum):
    QUEUED = "queued"
    DATA_PREPARING = "data_preparing"
    REDIS_PREPARING = "redis_preparing"
    MIGRATING = "migrating"
    SUBMITTING = "submitting"
    WAITING = "waiting"
    VERIFYING = "verifying"
    SWITCHING = "switching"
    TRAFFIC_WAITING = "traffic_waiting"
    HOLDING = "holding"
    AWAITING_PROMOTION = "awaiting_promotion"
    BAKING = "baking"
    RESTORING = "restoring"
    AWAITING_ROLLBACK = "awaiting_rollback"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class Action(StrEnum):
    PROMOTE = "promote"
    ROLLBACK = "rollback"


PRE_ROLLOUT_STATES = frozenset(
    {
        ReleaseState.QUEUED,
        ReleaseState.DATA_PREPARING,
        ReleaseState.REDIS_PREPARING,
        ReleaseState.MIGRATING,
    }
)
ROLLBACK_STATES = frozenset(
    {
        ReleaseState.VERIFYING,
        ReleaseState.HOLDING,
        ReleaseState.AWAITING_PROMOTION,
        ReleaseState.BAKING,
        ReleaseState.AWAITING_ROLLBACK,
    }
)


def available_actions(row) -> list[Action]:
    if row["runtime"] != Runtime.GCP_CLOUD_RUN:
        return []
    if row.get("checkpoint", {}).get("first_deployment"):
        return []
    state = row["state"]
    actions = [Action.PROMOTE] if state == ReleaseState.AWAITING_PROMOTION else []
    if state in ROLLBACK_STATES:
        actions.append(Action.ROLLBACK)
    return actions
