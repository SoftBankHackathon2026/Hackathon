from typing import Literal

from pydantic import Field, model_validator

from controlplane.domain.models import DeploymentRequest, ImageSource, RolloutPolicy, Strategy
from controlplane.domain.resources import CLOUD_RUN_SIZES, validate_resource_pair


class GcpDeploymentRequest(DeploymentRequest):
    source: ImageSource
    health_path: str = Field(default="/health", pattern=r"^/[A-Za-z0-9/_-]*$")
    cpu: Literal["1", "2"] = "1"
    architecture: Literal["X86_64"] = "X86_64"
    rollout: RolloutPolicy = Field(default_factory=RolloutPolicy)

    @model_validator(mode="after")
    def supported(self):
        if self.health_path.startswith("/_ah/") or self.health_path.endswith("z"):
            raise ValueError(
                "Cloud Run reserves /_ah/* and some paths ending in z; "
                "use an application endpoint such as /health or /ready"
            )
        if self.strategy not in {Strategy.CANARY, Strategy.BLUE_GREEN}:
            raise ValueError("Cloud Run supports canary and blue_green in this adapter")
        if self.secret_bindings:
            raise ValueError("GCP custom secrets are unsupported")
        if self.migration and self.resources.database != "postgresql":
            raise ValueError("GCP migration requires the managed dedicated PostgreSQL")
        if self.resources.database == "postgresql" and self.resources.database_mode != "dedicated":
            raise ValueError("GCP PostgreSQL requires dedicated mode")
        if self.cpu == "2" and self.memory_mib < 1024:
            raise ValueError("Two CPUs require at least 1024 MiB")
        validate_resource_pair(self.cpu, self.memory_mib, CLOUD_RUN_SIZES)
        return self
