from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from controlplane.domain.resources import Cpu, MemoryMiB


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Runtime(StrEnum):
    AWS_ECS = "aws_ecs"
    GCP_CLOUD_RUN = "gcp_cloud_run"
    ONPREM_ARGO = "onprem_argo"
    ONPREM_HELM = "onprem_helm"


class Strategy(StrEnum):
    BLUE_GREEN = "blue_green"
    CANARY = "canary"
    ROLLING = "rolling"


class GitSource(Model):
    kind: Literal["git"] = "git"
    repository: str = Field(max_length=512)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    dockerfile: str = "Dockerfile"
    context: str = "."

    @field_validator("repository")
    @classmethod
    def public_https_url(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("인증정보 없는 HTTPS 저장소 주소를 사용하세요.")
        return value

    @field_validator("dockerfile", "context")
    @classmethod
    def relative_path(cls, value: str) -> str:
        if (
            not value
            or len(value) > 256
            or "\\" in value
            or "\x00" in value
            or PurePosixPath(value).is_absolute()
            or ".." in PurePosixPath(value).parts
        ):
            raise ValueError("저장소 내부 상대 경로만 사용할 수 있습니다.")
        return value


class ImageSource(Model):
    kind: Literal["image"] = "image"
    image: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64}$")


class SecretBinding(Model):
    name: str = Field(pattern=r"^[A-Z_][A-Z0-9_]{0,62}$")
    # This is a logical binding name from the registered environment, never a secret value.
    binding: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")


class ResourceRequirements(Model):
    database: Literal["none", "postgresql"] = "none"
    redis: bool = False
    # shared: operator-prepared DB credentials. dedicated: the provider creates one per app.
    database_mode: Literal["shared", "dedicated", "container"] = "shared"


class Migration(Model):
    argv: list[str] = Field(min_length=1, max_length=32)
    timeout_seconds: int = Field(default=300, ge=10, le=1800)
    backward_compatible: bool

    @field_validator("argv")
    @classmethod
    def no_empty_arguments(cls, value: list[str]) -> list[str]:
        if any(not arg or len(arg) > 1024 or "\x00" in arg for arg in value):
            raise ValueError("명령을 실행 파일과 인자 배열로 전달하세요.")
        return value


class RolloutPolicy(Model):
    canary_percent: int = Field(default=10, ge=1, le=99, strict=True)
    hold_seconds: int = Field(default=300, ge=0, le=3600, strict=True)
    promotion: Literal["automatic", "manual"] = "automatic"
    auto_rollback: bool = True
    failure_threshold: int = Field(default=3, ge=2, le=10, strict=True)
    bake_seconds: int = Field(default=60, ge=15, le=600, strict=True)


class DeploymentRequest(Model):
    application: str = Field(pattern=r"^[a-z][a-z0-9-]{0,29}$")
    environment_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    source: Annotated[GitSource | ImageSource, Field(discriminator="kind")]
    port: int = Field(default=8080, ge=1, le=65535)
    health_path: str = Field(default="/health", pattern=r"^/[A-Za-z0-9/_-]*$")
    strategy: Strategy = Strategy.BLUE_GREEN
    cpu: Cpu = "1"
    memory_mib: MemoryMiB = 1024
    replicas: int = Field(default=1, ge=1, le=5)
    architecture: Literal["X86_64", "ARM64"] = "X86_64"
    resources: ResourceRequirements = Field(default_factory=ResourceRequirements)
    secret_bindings: list[SecretBinding] = Field(default_factory=list, max_length=30)
    env: dict[str, str] = Field(default_factory=dict, max_length=50)
    migration: Migration | None = None
    rollout: RolloutPolicy | None = None
    # Zero-code OpenTelemetry instrumentation the platform injects at deploy time.
    tracing_enabled: bool | None = None
    instrumentation: Literal["auto", "python", "java", "nodejs", "dotnet", "sdk"] | None = None

    @model_validator(mode="after")
    def validate_bindings(self):
        import re

        names = [s.name for s in self.secret_bindings]
        reserved = {"PORT", "LAUNCHPAD_RELEASE_ID", "DATABASE_URL", "REDIS_URL", *names}
        if any(
            not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,62}", k)
            or k in reserved
            or len(v) > 4096
            or "\x00" in v
            for k, v in self.env.items()
        ):
            raise ValueError("Invalid, reserved or duplicate environment variable")
        if len(set(names)) != len(names) or {"PORT", "LAUNCHPAD_RELEASE_ID"}.intersection(names):
            raise ValueError(
                "환경변수 이름은 중복할 수 없고 PORT와 LAUNCHPAD_RELEASE_ID는 플랫폼이 관리합니다."
            )
        if self.migration and (self.resources is None or self.resources.database == "none"):
            raise ValueError("마이그레이션에는 DB 요구사항을 지정하세요.")
        if self.migration and not self.migration.backward_compatible:
            raise ValueError("기존 버전과 호환되는 마이그레이션만 첫 버전에서 지원합니다.")
        return self


class Environment(Model):
    id: str
    name: str
    runtime: Runtime
    state: Literal["unregistered", "provisioning", "ready", "failed"] = "unregistered"
    preparation: Literal["preprovisioned", "on_demand"] = "preprovisioned"
    resource_refs: dict[str, str] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    example_only: bool = True


class Step(Model):
    id: str
    title: str
    owner: Literal["platform", "build", "terraform", "provider", "application"]
    detail: str


class Plan(Model):
    mode: Literal["plan_only"] = "plan_only"
    executable: Literal[False] = False
    environment_id: str
    runtime: Runtime
    application: str
    fingerprint: str
    blockers: list[str]
    steps: list[Step]
    notes: list[str]
