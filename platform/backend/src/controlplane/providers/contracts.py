"""Worker-facing SDK contracts. These are not public HTTP request models."""

from enum import StrEnum
from typing import Literal

from pydantic import Field

from controlplane.domain.models import Model


class Phase(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    UNKNOWN = "unknown"


class Observation(Model):
    phase: Phase
    resource_id: str
    code: str
    # Provider readiness is NOT application health or a completed release.
    application_verified: Literal[False] = False


class Submission(Model):
    provider: Literal["gcp_cloud_run", "aws_ecs", "onprem_argo"]
    resource_id: str
    operation_id: str | None = None
    recovered: bool = False


class ReleaseKey(Model):
    value: str = Field(pattern=r"^[0-9a-f]{24}$")


class ProviderConflict(RuntimeError):
    """Observed state no longer matches the approved release snapshot."""


class SubmissionUncertain(RuntimeError):
    """The write may have reached the provider. Reconcile; do NOT blindly retry."""


class ProviderRejected(RuntimeError):
    """A definite provider rejection. Messages intentionally exclude raw SDK payloads."""


class ProviderUnavailable(RuntimeError):
    """A bounded read failed transiently; defer observation without changing desired state."""
