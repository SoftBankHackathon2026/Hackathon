from enum import StrEnum


class ArgoState(StrEnum):
    QUEUED = "queued"
    INITIALIZING = "initializing"
    INITIALIZED = "initialized"
    MIGRATION_SUBMITTING = "migration_submitting"
    MIGRATION_WAITING = "migration_waiting"
    PREPARING = "preparing"
    SUBMITTING = "submitting"
    WAITING = "waiting"
    VERIFYING = "verifying"
    PROMOTING = "promoting"
    CHECKING = "checking"
    RESTORE_SUBMITTING = "restore_submitting"
    RESTORE_WAITING = "restore_waiting"
    RESTORE_VERIFYING = "restore_verifying"
    RESTORE_PROMOTING = "restore_promoting"
    RESTORE_CHECKING = "restore_checking"
    UNPUBLISHING = "unpublishing"


RESTORE_PREFIX = "restore_"


class DrivePhase(StrEnum):
    WAITING = "waiting"
    VERIFYING = "verifying"
    PROMOTING = "promoting"
    CHECKING = "checking"
