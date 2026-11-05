from typing import Literal

from pydantic import Field, field_validator, model_validator

from controlplane.builds.models import BuildInput
from controlplane.core.config import PlatformEnvironment
from controlplane.domain.models import Model

Runtime = Literal["aws_ecs", "gcp_cloud_run", "onprem_helm"]
RUNTIMES = ("aws_ecs", "gcp_cloud_run", "onprem_helm")
KEY = r"^[A-Za-z0-9_-]{16,128}$"


class TargetOverride(Model):
    """What one destination may change from its registered defaults for this run."""

    strategy: Literal["rolling", "blue_green", "canary"] | None = None
    cpu: str | None = Field(default=None, pattern=r"^[0-9]{1,2}(\.[0-9]{1,3})?$")
    memory_mib: int | None = Field(default=None, ge=128, le=65536)
    port: int | None = Field(default=None, ge=1024, le=65535)
    health_path: str | None = Field(default=None, pattern=r"^/[A-Za-z0-9/_-]*$")


class PreviewInput(Model):
    repository: str
    dockerfile: str = "Dockerfile"
    context: str = "."
    revision: str | None = Field(default=None, min_length=1, max_length=128)
    port: int | None = Field(default=None, ge=1024, le=65535)
    health_path: str | None = Field(default=None, pattern=r"^/[A-Za-z0-9/_-]*$")
    # A catalog app: deploy to its own environment on each provider instead of the
    # operator's fixed demo destinations. The caller must own the app.
    application_id: str | None = Field(default=None, pattern=r"^app-[a-f0-9]{20}$")
    # Per-destination changes; the provider still validates the resulting request.
    overrides: dict[Runtime, TargetOverride] = Field(default_factory=dict)

    @field_validator("repository")
    @classmethod
    def repository_url(cls, value):
        return BuildInput.repository_url(value)

    @field_validator("revision")
    @classmethod
    def revision_name(cls, value):
        return BuildInput.revision_name(value) if value is not None else None

    @field_validator("dockerfile", "context")
    @classmethod
    def source_path(cls, value):
        return BuildInput.source_path(value)


class StartRun(Model):
    preview_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    idempotency_key: str = Field(pattern=KEY)


class DemoDeployInput(PreviewInput):
    idempotency_key: str = Field(pattern=KEY)


class RetryRun(Model):
    runtimes: list[Runtime] = Field(min_length=1, max_length=3)
    idempotency_key: str = Field(pattern=KEY)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.runtimes)) != len(self.runtimes):
            raise ValueError("Duplicate retry target")
        return self


class RunSettings(Model):
    """Only operator files can choose default destinations, never repository content."""

    # Fixed demo destinations. Optional: runs for a catalog app bring their own.
    targets: dict[Runtime, str] = Field(default_factory=dict)
    max_pending: int | None = Field(default=None, ge=1)
    poll_seconds: int = Field(default=5, ge=1, le=30)

    @model_validator(mode="after")
    def three_targets(self):
        if not self.targets:
            return self
        if set(self.targets) != set(RUNTIMES) or len(set(self.targets.values())) != 3:
            raise ValueError("Configure exactly one distinct target for each provider")
        if any(not value or len(value) > 80 for value in self.targets.values()):
            raise ValueError("Invalid target ID")
        return self

    @classmethod
    def from_environment(cls) -> "RunSettings | None":
        path = PlatformEnvironment().demo_deploy_config
        return cls.model_validate_json(path.read_text()) if path else None
