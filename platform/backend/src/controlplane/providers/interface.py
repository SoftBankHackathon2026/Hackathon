"""Public asynchronous deployment port. SDK details and worker checkpoints stay private."""

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from controlplane.domain.models import (
    DeploymentRequest,
    GitSource,
    ImageSource,
    Model,
    Plan,
    ResourceRequirements,
    Runtime,
)
from controlplane.domain.resources import Cpu, MemoryMiB
from controlplane.providers.capabilities import ProviderCapabilities


class RepositorySource(Model):
    """A build from the repository's default branch; not equivalent to pinned GitSource."""

    kind: Literal["repository"] = "repository"
    repository: str = Field(min_length=1, max_length=2048)


class DeploymentSpec(DeploymentRequest):
    """One input for all providers. Omitted settings use provider defaults/preserve policy.

    Optional fields are deliberately None, not invented Cloud Run resource defaults.
    Providers reject unsupported explicitly requested fields rather than discard them.
    """

    source: ImageSource | GitSource | RepositorySource = Field(discriminator="kind")
    port: int | None = Field(default=None, ge=1, le=65535)
    health_path: str | None = Field(default=None, pattern=r"^/[A-Za-z0-9/_-]*$")
    strategy: Literal["blue_green", "canary", "rolling"] | None = None
    cpu: Cpu | None = None
    memory_mib: MemoryMiB | None = None
    replicas: int | None = Field(default=None, ge=1, le=5)
    architecture: Literal["X86_64", "ARM64"] | None = None

    resources: ResourceRequirements | None = None

    def requested(self) -> dict:
        payload = self.model_dump(mode="json", exclude_unset=True)
        # Nested model defaults can omit a required union discriminator.
        payload["source"]["kind"] = self.source.kind
        return payload


class DeploymentOperation(Model):
    id: str = Field(pattern=r"^[0-9a-f]{24}$")
    application: str
    environment_id: str
    runtime: Runtime


class DeploymentEvent(Model):
    phase: str
    message: str
    created_at: datetime


class DeploymentStatus(DeploymentOperation):
    status: Literal["queued", "running", "succeeded", "failed", "uncertain"]
    phase: str
    image: str | None = None
    repository: str | None = None
    service_url: str | None = None
    service_access: Literal["public", "authenticated"] = "public"
    error_code: str | None = None
    # Set in login mode: the team that owns the release and who requested it.
    team: str | None = None
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime
    events: list[DeploymentEvent] = Field(default_factory=list)
    actions: list[Literal["promote", "rollback"]] = Field(default_factory=list)
    # Set when this release re-deploys an earlier succeeded release (rollback to a version).
    restore_of: str | None = None
    # Optional, allowlisted repository bridge observations. Native providers keep their contract.
    project_id: str | None = None
    source_commit: str | None = None
    image_uri: str | None = None
    digest: str | None = None
    revision: str | None = None
    previous_revision: str | None = None
    candidate_revision: str | None = None
    active_revision: str | None = None
    health: Literal["OK"] | None = None
    rollback_status: Literal["SUCCESS", "FAILED", "UNCERTAIN", "NOT_REQUIRED"] | None = None


class AppLogLine(Model):
    at: datetime
    stream: str | None = None
    message: str


class AppLogs(Model):
    """Application output read from the provider's log store, outside the platform DB."""

    application: str
    environment_id: str
    minutes: int
    max_lookback_minutes: int = 360
    lines: list[AppLogLine]
    truncated: bool = False


@runtime_checkable
class ReleaseActionProvider(Protocol):
    def request_action(self, operation: DeploymentOperation, action: str) -> DeploymentStatus: ...


@runtime_checkable
class ReleaseRestoreProvider(Protocol):
    """Re-deploy an earlier succeeded release of this binding as a new release."""

    def restore(
        self,
        operation: DeploymentOperation,
        operation_key: str,
        *,
        team: str | None = None,
        created_by: str | None = None,
    ) -> DeploymentOperation: ...


@runtime_checkable
class AppLogProvider(Protocol):
    def app_logs(self, *, minutes: int, filter_text: str | None, limit: int) -> AppLogs: ...


@runtime_checkable
class DeploymentProvider(Protocol):
    runtime: Runtime
    application: str
    environment_id: str

    def initialize(self) -> None: ...
    def capabilities(self) -> ProviderCapabilities: ...
    def validate(self, spec: DeploymentSpec) -> None: ...
    def plan(self, spec: DeploymentSpec) -> Plan: ...
    def submit(
        self,
        spec: DeploymentSpec,
        operation_key: str,
        *,
        team: str | None = None,
        created_by: str | None = None,
    ) -> DeploymentOperation: ...
    def observe(self, operation: DeploymentOperation) -> DeploymentStatus: ...
    def public_environment(self) -> dict: ...
    def register_application(self, **settings) -> dict: ...
