from pathlib import Path
from typing import Literal

from pydantic import Field

from controlplane.domain.models import DeploymentRequest, ImageSource
from controlplane.providers.onprem.digest import sha256_json
from controlplane.providers.onprem.helm.models import (
    ChartArtifact,
    HelmEnvironment,
    PreviousHelmBinding,
)
from controlplane.providers.onprem.helm.plan import strategy_values
from controlplane.providers.onprem.observability import (
    ObservabilitySettings,
    instrumentation_values,
)
from controlplane.providers.public_images import parse_public_image
from controlplane.providers.settings import ProviderSettings

REVIEWED_APP_VERSIONS = {"0.1.1", "0.2.0", "0.3.0"}
REVIEWED_DATA_VERSION = "0.1.1"
FINGERPRINT_EXCLUDED = {
    "database_url",
    "kubeconfig",
    "allowed_images",
    "binary",
    "default_strategy",
    "demo_database",
    "catalog_target_id",
    "catalog_max_apps",
    "catalog_dynamic",
}


class HelmSettings(ProviderSettings):
    engine: Literal["helm"] = "helm"
    catalog_target_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,39}$")
    # Legacy runtime files and saved bindings may contain this field; no longer enforced.
    catalog_max_apps: int | None = Field(default=None, ge=1)
    catalog_dynamic: bool = False
    chart_source_commit: str = Field(
        default="d0ec372a9a487012ca8e2dd5ff2f609896ad62ea", pattern=r"^[a-f0-9]{40}$"
    )
    default_strategy: Literal["blue_green", "canary"] = "blue_green"
    demo_database: bool = True
    required_database: Literal["none", "postgresql"] = "none"
    environment: HelmEnvironment
    kubeconfig: Path
    context: str
    server: str
    binary: Path
    app_chart: ChartArtifact
    data_chart: ChartArtifact
    allowed_images: list[str] = Field(min_length=1)
    poll_seconds: int = Field(default=5, ge=1, le=30)
    release_timeout_seconds: int = Field(default=1800, ge=600, le=3600)
    required_health_samples: int = 3
    production_check_limit: int = 12
    observability: ObservabilitySettings | None = None
    previous_bindings: list[PreviousHelmBinding] = Field(default_factory=list, max_length=10)

    def fingerprint(self):
        value = self.model_dump(mode="json", exclude=FINGERPRINT_EXCLUDED)
        if self.catalog_dynamic:
            value["catalog_dynamic"] = True
        for key in ("app_chart", "data_chart"):
            value[key].pop("path")
        if value["observability"] is None:
            value.pop("observability")
        if not value["previous_bindings"]:
            value.pop("previous_bindings")
        else:
            for previous in value["previous_bindings"]:
                previous["app_chart"].pop("path")
        return sha256_json(value)

    def accepted_bindings(self):
        return [self.fingerprint(), *[b.fingerprint for b in self.previous_bindings]]

    def _validate_image(self, request) -> None:
        if not isinstance(request.source, ImageSource) or request.architecture != "X86_64":
            raise ValueError("amd64 digest image required")
        if self.catalog_dynamic:
            parse_public_image(request.source.image)
        elif request.source.image not in self.allowed_images and not self.built_image_allowed(
            request
        ):
            raise ValueError("Image not approved for this binding")

    def _validate_instrumentation(self, request) -> None:
        if request.instrumentation is None:
            return
        if not self.app_chart.supports_instrumentation:
            raise ValueError("Instrumentation requires the reviewed 0.2.0 application chart")
        if self.app_chart.version == "0.2.0" and request.instrumentation != "python":
            raise ValueError("Multi-language instrumentation requires chart 0.3.0")
        instrumentation_values(self, request, "validation")

    def _validate_data_policy(self, request) -> None:
        if self.required_database == "postgresql" and request.resources.database != "postgresql":
            raise ValueError("This application requires PostgreSQL")
        if request.resources.database != "none" and request.resources.database_mode != "container":
            raise ValueError("Helm database mode must be container")
        if request.secret_bindings:
            raise ValueError("This binding manages its data credentials automatically")
        if request.migration and not request.migration.backward_compatible:
            raise ValueError("Migration must preserve compatibility with the active app")

    def _validate_charts(self) -> None:
        if (
            self.app_chart.name != "oneport-app"
            or self.data_chart.name != "oneport-data"
            or self.app_chart.version not in REVIEWED_APP_VERSIONS
            or self.data_chart.version != REVIEWED_DATA_VERSION
        ):
            raise ValueError("Worker requires reviewed app 0.1.1/0.2.0 and data 0.1.1 packages")

    def validate_request(self, request):
        env = self.environment
        if (request.application, request.environment_id) != (env.application, env.environment_id):
            raise ValueError("Wrong Helm binding")
        self._validate_image(request)
        self._validate_instrumentation(request)
        strategy_values(request, env)
        if request.port < 1024 or env.exceeds_ceiling(request):
            raise ValueError("Resource limits exceeded")
        self._validate_data_policy(request)
        self._validate_charts()

    def demo_request(self):
        return DeploymentRequest(
            application=self.environment.application,
            environment_id=self.environment.environment_id,
            source=ImageSource(
                image="docker.io/oneport/application@sha256:" + "0" * 64
                if self.catalog_dynamic
                else self.allowed_images[0]
            ),
            strategy=self.default_strategy,
            port=8080,
            health_path="/health",
            cpu="0.25",
            memory_mib=256,
            resources={
                "database": "postgresql" if self.demo_database else "none",
                "database_mode": "container",
            },
            migration={"argv": ["sh", "scripts/start.sh", "migrate"], "backward_compatible": True}
            if self.demo_database
            else None,
        )
