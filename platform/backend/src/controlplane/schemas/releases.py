from typing import Literal

from controlplane.domain.models import Model
from controlplane.providers.interface import DeploymentSpec
from controlplane.schemas.common import IdempotencyKey, PlanFingerprint


class SubmitRelease(Model):
    request: DeploymentSpec
    plan_fingerprint: PlanFingerprint | None = None
    idempotency_key: IdempotencyKey


class ReleaseAction(Model):
    action: Literal["promote", "rollback"]


class RestoreRelease(Model):
    idempotency_key: IdempotencyKey
