"""Explicit operator binding and bounded health checks for the AWS worker.

Same role as execution.kubernetes_runtime: trusted config file in, SDK clients,
CandidateGateway and HTTP probe out. Shared infra values and the access key are
read from the Notion `.env` file named in the config, never from HTTP users.
"""

import hashlib
import json
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field

from controlplane.domain.models import DeploymentRequest
from controlplane.providers.contracts import ReleaseKey
from controlplane.providers.settings import ProviderSettings
from oneport_aws.candidates import CandidateGateway
from oneport_aws.providers.oneport_ecs.connection import connect
from oneport_aws.providers.oneport_ecs.driver import OneportEcsCandidateDriver
from oneport_aws.providers.oneport_ecs.models import (
    OneportEcsEnvironment,
    credentials_from_env_file,
    environment_from_env_file,
)
from oneport_aws.providers.oneport_ecs.provider import OneportEcsProvider
from oneport_aws.providers.oneport_ecs.validation import validate_deployment


class OneportEcsSettings(ProviderSettings):
    catalog_target_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,62}$")
    catalog_dynamic: bool = False
    env_file: Path  # Notion `.env` (shared values + oneport-platform-dev key)
    application: str = Field(pattern=r"^[a-z][a-z0-9-]{0,29}$")
    environment_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    required_database: Literal["none", "postgresql"] = "none"
    secret_refs: dict[str, str] = Field(default_factory=dict)
    max_replicas: int = Field(default=2, ge=1, le=5)
    max_cpu: float = Field(default=1, gt=0, le=2)
    max_memory_mib: int = Field(default=2048, ge=512, le=4096)
    bake_minutes: int = Field(default=5, ge=1, le=60)
    allowed_images: list[str] = Field(default_factory=list)  # empty = any digest in the app repo
    poll_seconds: int = Field(default=10, ge=1, le=30)
    required_health_samples: int = Field(default=3, ge=2, le=10)
    production_check_limit: int = Field(default=12, ge=3, le=60)
    # Accepted for old runtime files only. Dedicated DB and cache provisioning now
    # follow the app registration (web or CLI); this flag no longer gates them.
    legacy_dedicated_provisioning: bool = False
    db_settle_seconds: int = Field(default=60, ge=0, le=600)
    cache_settle_seconds: int = Field(default=30, ge=0, le=600)

    def environment(self) -> OneportEcsEnvironment:
        return environment_from_env_file(
            self.env_file,
            application=self.application,
            environment_id=self.environment_id,
            secret_refs=self.secret_refs,
            public_images=[
                image for image in self.allowed_images if image.startswith("docker.io/")
            ],
            required_database=self.required_database,
            max_replicas=self.max_replicas,
            max_cpu=self.max_cpu,
            max_memory_mib=self.max_memory_mib,
            bake_minutes=self.bake_minutes,
        )

    def fingerprint(self) -> str:
        # Credentials rotate independently; infra binding and policy must not drift.
        value = {
            "environment": self.environment().model_dump(mode="json", exclude={"public_images"}),
            "policy": self.model_dump(
                mode="json",
                exclude={
                    "database_url",
                    "env_file",
                    "allowed_images",
                    "catalog_target_id",
                    "catalog_dynamic",
                },
            ),
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def validate_request(
        self,
        request: DeploymentRequest,
        provider: OneportEcsProvider | None = None,
        extra_refs: dict[str, str] | None = None,
    ) -> None:
        image = request.source.image
        built = image not in self.allowed_images and self.built_image_allowed(request)
        if provider is None:
            env = self.environment()
            env = env.model_copy(update={"secret_refs": {**env.secret_refs, **(extra_refs or {})}})
            validate_deployment(env.with_public_image(image) if built else env, request)
        else:
            if built:
                provider.allow_public_image(image)
            provider.validate(request)
        if self.allowed_images and image not in self.allowed_images and not built:
            raise ValueError("등록되지 않은 이미지 digest입니다.")


class OneportEcsRuntime:
    def __init__(self, settings: OneportEcsSettings):
        self.settings = settings
        self.provider: OneportEcsProvider | None = None
        self.driver: OneportEcsCandidateDriver | None = None
        self.gateway: CandidateGateway | None = None
        self.http: httpx.Client | None = None

    def connect(self) -> None:
        if self.provider is not None:
            return
        env = self.settings.environment()
        clients = connect(credentials_from_env_file(self.settings.env_file), env.region)
        self.provider = OneportEcsProvider(clients, env)
        self.driver = OneportEcsCandidateDriver(self.provider)
        self.gateway = CandidateGateway({(env.application, env.environment_id): self.driver})
        self.http = httpx.Client(
            timeout=httpx.Timeout(5, connect=3), follow_redirects=False, trust_env=False
        )

    def probe(self, request: DeploymentRequest, release: ReleaseKey) -> bool:
        """Production URL only: ECS has already shifted traffic (no preview route)."""
        url = self.provider.environment.url + request.health_path
        try:
            with self.http.stream("GET", url) as response:
                return (
                    response.status_code == 200
                    and response.headers.get("X-Launchpad-Release") == release.value
                )
        except httpx.HTTPError:
            return False

    def close(self):
        if self.http:
            self.http.close()
