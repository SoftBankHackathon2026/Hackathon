import re
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator

from controlplane.core.config import PlatformEnvironment
from controlplane.domain.models import GitSource, Model
from controlplane.providers.interface import DeploymentSpec


class BuildState(StrEnum):
    QUEUED = "queued"
    RESOLVED = "resolved"
    SUBMITTING = "submitting"
    BUILDING = "building"
    DEPLOYING = "deploying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class BuildInput(Model):
    repository: str
    revision: str = Field(default="main", min_length=1, max_length=128)
    dockerfile: str = "Dockerfile"
    context: str = "."
    deployment: DeploymentSpec
    plan_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    deploy_after_build: bool = True
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")

    @field_validator("repository")
    @classmethod
    def repository_url(cls, value):
        match = re.fullmatch(
            r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,104})/?",
            value.strip(),
        )
        if not match:
            raise ValueError("Public GitHub repository URL required")
        owner, name = match.groups()
        name = name.removesuffix(".git")
        if name in ("", ".", "..") or len(name) > 100:
            raise ValueError("Public GitHub repository URL required")
        return f"https://github.com/{owner}/{name}"

    @field_validator("revision")
    @classmethod
    def revision_name(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", value) or ".." in value:
            raise ValueError("Invalid Git revision")
        return value

    @field_validator("dockerfile", "context")
    @classmethod
    def source_path(cls, value):
        return GitSource.relative_path(value)


class BuildSettings(Model):
    project: str = Field(pattern=r"^oneport-[a-z0-9-]+$")
    region: str = "ap-northeast-2"
    image_repository: str = Field(pattern=r"^docker\.io/[a-z0-9_-]+/[a-z0-9_.-]+$")
    # Optional UI suggestions retained for existing runtime files, never an allowlist.
    repositories: list[str] = Field(default_factory=list)
    required_databases: dict[str, Literal["postgresql"]] = Field(default_factory=dict)
    poll_seconds: int = Field(default=5, ge=1, le=30)
    # None disables platform quotas; provider account quotas still apply.
    max_pending: int | None = Field(default=None, ge=1)
    max_daily: int | None = Field(default=None, ge=1)

    @classmethod
    def from_environment(cls) -> "BuildSettings | None":
        path = PlatformEnvironment().build_config
        return cls.model_validate_json(path.read_text()) if path else None
