import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from controlplane.domain.models import Model
from controlplane.providers.gcp.origin import is_registered_origin
from controlplane.providers.gcp.tracing import COLLECTOR_IMAGE
from controlplane.providers.settings import ProviderSettings


class GcpEnvironment(Model):
    application: str = Field(pattern=r"^[a-z][a-z0-9-]{0,29}$")
    environment_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")
    region: str = Field(pattern=r"^[a-z]+-[a-z]+[0-9]$")
    service: str = Field(pattern=r"^[a-z][a-z0-9-]{0,36}[a-z0-9]$")
    service_uid: str | None = Field(default=None, min_length=1, max_length=128)
    service_url: str | None = None
    max_replicas: int = Field(default=5, ge=1, le=5)
    max_cpu: int = Field(default=2, ge=1, le=2)
    max_memory_mib: int = Field(default=4096, ge=512, le=4096)

    @model_validator(mode="after")
    def origin(self):
        if self.service_url is None:
            return self
        if not is_registered_origin(self.service_url):
            raise ValueError("Register an HTTPS service origin, without a path or credentials")
        return self

    @property
    def service_name(self):
        return f"projects/{self.project_id}/locations/{self.region}/services/{self.service}"


class GcpDataSettings(Model):
    """Operator-owned names from infra/gcp outputs; callers cannot choose a network/account."""

    app_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,18}$")
    network: str = Field(pattern=r"^projects/[a-z0-9-]+/global/networks/[a-z0-9-]+$")
    subnetwork: str = Field(
        pattern=r"^projects/[a-z0-9-]+/regions/[a-z0-9-]+/subnetworks/[a-z0-9-]+$"
    )
    allocated_ip_range: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")

    def fingerprint(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class GcpCacheSettings(Model):
    """Opt-in to the fixed infra/gcp cache workflow, never caller-selected resources."""

    app_id: str = Field(pattern=r"^[a-z]([a-z0-9-]{0,17}[a-z0-9])?$")
    network: str = Field(pattern=r"^projects/[a-z0-9-]+/global/networks/[a-z0-9-]+$")
    subnetwork: str = Field(
        pattern=r"^projects/[a-z0-9-]+/regions/[a-z0-9-]+/subnetworks/[a-z0-9-]+$"
    )

    def fingerprint(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class GcpTracingSettings(Model):
    sample_ratio: float = Field(default=1.0, ge=0, le=1)
    python_platform: Literal["glibc", "musl"] = "glibc"
    agent_images: dict[Literal["java", "nodejs", "dotnet"], str] = Field(default_factory=dict)

    @field_validator("agent_images")
    @classmethod
    def pinned_agents(cls, value):
        if any(not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image) for image in value.values()):
            raise ValueError("Agent images must be digest pinned")
        return value

    # Operator-owned pinned images, never supplied by a deployment request.
    collector_image: str = Field(default="", pattern=r"^.*@sha256:[0-9a-f]{64}$")
    python_image: str | None = Field(default=None, pattern=r"^.*@sha256:[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def images(cls, value):
        if not isinstance(value, dict):
            return value
        return {"collector_image": COLLECTOR_IMAGE, **value}


class GcpSettings(ProviderSettings):
    catalog_target_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,62}$")
    catalog_dynamic: bool = False
    catalog_owner: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    engine: Literal["cloud_run"] = "cloud_run"
    credentials_file: Path | None = None
    environment: GcpEnvironment
    service_access: Literal["public", "authenticated"] = "authenticated"
    allowed_images: list[str] = Field(min_length=1)
    deployer_service_account: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9-]+@[a-zA-Z0-9-]+\.iam\.gserviceaccount\.com$"
    )
    poll_seconds: int = Field(default=5, ge=1, le=30)
    required_health_samples: int = Field(default=3, ge=2, le=10)
    production_check_limit: int = Field(default=12, ge=3, le=60)
    release_timeout_seconds: int = Field(default=7200, ge=4800, le=86400)
    data_provisioning: GcpDataSettings | None = None
    cache_provisioning: GcpCacheSettings | None = None
    tracing: GcpTracingSettings | None = None

    @model_validator(mode="after")
    def health_limits(self):
        if self.catalog_dynamic:
            if not self.catalog_owner or len(self.environment.application) > 19:
                raise ValueError("Dynamic Cloud Run bindings need an owner and a short app ID")
        elif not self.environment.service_uid or not self.environment.service_url:
            raise ValueError("Register the existing Cloud Run service UID and HTTPS origin")
        if self.production_check_limit < self.required_health_samples:
            raise ValueError("Health checks must allow the required consecutive successes")
        for data in (self.data_provisioning, self.cache_provisioning):
            if data is None:
                continue
            env = self.environment
            if not data.network.startswith(f"projects/{env.project_id}/") or not (
                data.subnetwork.startswith(f"projects/{env.project_id}/regions/{env.region}/")
            ):
                raise ValueError("Data networking must belong to the registered project/region")
        if self.data_provisioning and self.cache_provisioning:
            for field in ("app_id", "network", "subnetwork"):
                if getattr(self.data_provisioning, field) != getattr(
                    self.cache_provisioning, field
                ):
                    raise ValueError("DB and cache must use the same registered app and network")
        return self

    def fingerprint(self):
        # Enabling optional data provisioning must not invalidate image-only releases
        # already waiting for promotion. Data has its own persisted policy fingerprint.
        value = self.model_dump(
            mode="json",
            exclude={
                "database_url",
                "allowed_images",
                "data_provisioning",
                "cache_provisioning",
                "tracing",
                "credentials_file",
                "catalog_target_id",
                "catalog_dynamic",
                "catalog_owner",
            },
        )
        if self.service_access == "authenticated":
            value.pop("service_access")
        if self.catalog_dynamic:
            value["catalog_owner"] = self.catalog_owner
            value["environment"].pop("service_uid")
            value["environment"].pop("service_url")
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    @property
    def resource_owner(self):
        return self.catalog_owner if self.catalog_dynamic else self.environment.service_uid

    def validate_request(self, request):
        if request.instrumentation and self.tracing is None:
            raise ValueError("이 GCP 환경에는 앱 추적 설정이 등록되지 않았습니다.")
        if (
            request.instrumentation in {"java", "nodejs", "dotnet"}
            and request.instrumentation not in self.tracing.agent_images
        ):
            raise ValueError("이 언어의 자동 계측 이미지가 아직 등록되지 않았습니다.")
        if self.tracing and any(k.startswith("OTEL_") for k in request.env):
            raise ValueError("OTEL_* variables are managed by the platform when tracing is enabled")
        if request.resources.database != "none" and self.data_provisioning is None:
            raise ValueError("이 GCP 환경에는 DB 생성 설정이 등록되지 않았습니다.")
        if request.resources.redis and self.cache_provisioning is None:
            raise ValueError("이 GCP 환경에는 캐시 생성 설정이 등록되지 않았습니다.")
        if request.resources.redis and {"CACHE_CONFIG", "REDIS_URL"}.intersection(request.env):
            raise ValueError("CACHE_CONFIG·REDIS_URL은 플랫폼이 Secret으로 관리합니다.")
        env = self.environment
        if (request.application, request.environment_id) != (env.application, env.environment_id):
            raise ValueError("Request does not match the registered Cloud Run service")
        if request.source.image not in self.allowed_images and not self.built_image_allowed(
            request
        ):
            raise ValueError("Image digest is not registered")
        if (
            int(request.cpu) > env.max_cpu
            or request.memory_mib > env.max_memory_mib
            or request.replicas > env.max_replicas
        ):
            raise ValueError("Request exceeds the registered environment limits")
