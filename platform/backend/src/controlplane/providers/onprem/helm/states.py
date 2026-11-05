from enum import StrEnum


class HelmState(StrEnum):
    QUEUED = "queued"
    DATA_PREPARING = "data_preparing"
    DATA_SUBMITTING = "data_submitting"
    DATA_WAITING = "data_waiting"
    DATA_TESTING = "data_testing"
    MIGRATION_READY = "migration_ready"
    MIGRATION_SUBMITTING = "migration_submitting"
    MIGRATION_WAITING = "migration_waiting"
    APP_READY = "app_ready"
    PUBLISH_READY = "publish_ready"
    APP_SUBMITTING = "app_submitting"
    APP_WAITING = "app_waiting"
    CANARY_WAITING = "canary_waiting"
    STRATEGY_WAITING = "strategy_waiting"
    APP_TESTING = "app_testing"
    CHECKING = "checking"
    RESTORE_SUBMITTING = "restore_submitting"
    RESTORE_CHECKING = "restore_checking"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class ReleaseKind(StrEnum):
    APP = "app"
    DATA = "data"
