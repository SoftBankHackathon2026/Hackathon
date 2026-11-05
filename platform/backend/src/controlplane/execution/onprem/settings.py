from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from controlplane.domain.models import DeploymentRequest, ImageSource
from controlplane.providers.onprem import pods
from controlplane.providers.onprem.connection import is_credential_free_https
from controlplane.providers.onprem.digest import sha256_json
from controlplane.providers.onprem.models import ArgoEnvironment
from controlplane.providers.onprem.observability import ObservabilitySettings
from controlplane.providers.settings import ProviderSettings


class ArgoSettings(ProviderSettings):
    engine: Literal["argo"] = "argo"
    environment: ArgoEnvironment
    kubeconfig: Path
    context: str = Field(min_length=1)
    server: str
    allowed_images: list[str] = Field(min_length=1)
    health_ca_file: Path | None = None
    poll_seconds: int = Field(default=5, ge=1, le=30)
    required_health_samples: int = Field(default=3, ge=2, le=10)
    production_check_limit: int = Field(default=12, ge=3, le=60)
    preview_probe_mode: Literal["http", "job"] = "http"
    preview_probe_image: str | None = None
    observability: ObservabilitySettings | None = None

    @model_validator(mode="after")
    def validate_policy(self):
        if not is_credential_free_https(self.server):
            raise ValueError("Explicit HTTPS Kubernetes API required")
        if self.production_check_limit < self.required_health_samples:
            raise ValueError("Production checks must allow the required consecutive successes")
        if self.preview_probe_mode == "job":
            if not self.preview_probe_image:
                raise ValueError("A pinned Python probe image is required for preview Job mode")
            ImageSource(image=self.preview_probe_image)
        return self

    def fingerprint(self):
        # Credentials rotate independently; API identity, app binding and policy must not drift.
        value = self.model_dump(
            mode="json", exclude={"database_url", "kubeconfig", "allowed_images", "observability"}
        )
        if self.preview_probe_mode == "http":
            # Existing releases predate optional Job probing. Preserve their default-mode hash.
            value.pop("preview_probe_mode")
            value.pop("preview_probe_image")
        return sha256_json(value)

    def validate_request(self, request: DeploymentRequest):
        pods.validate(request, self.environment)
        if any(
            self.environment.secret_refs[b.binding].uid is None for b in request.secret_bindings
        ):
            raise ValueError("Pin Secret UIDs from preflight before enqueuing a live release")
        if request.source.image not in self.allowed_images and not self.built_image_allowed(
            request
        ):
            raise ValueError("Image digest is not registered for this environment")
