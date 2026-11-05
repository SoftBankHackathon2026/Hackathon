from typing import Literal

from pydantic import Field, model_validator

from controlplane.domain.models import DeploymentRequest, Model
from controlplane.providers.onprem.bindings import DNS, HOST, KubernetesBinding


class ArgoEnvironment(KubernetesBinding):
    namespace: str = Field(default="oneport-apps", pattern=DNS)
    ingress_class: Literal["traefik"] = "traefik"
    workload_service_account: str = Field(default="oneport-runtime", pattern=DNS)
    cluster_domain: str = Field(default="cluster.local", pattern=HOST, max_length=200)
    scale_down_delay_seconds: int = Field(default=300, ge=30, le=600)


class ArgoDeploymentRequest(DeploymentRequest):
    @model_validator(mode="after")
    def reject_rollout(self):
        if self.rollout is not None:
            raise ValueError("Configurable traffic rollout is not supported by the Argo adapter")
        return self


class Revision(Model):
    uid: str
    resource_version: str
    generation: int
    release: str

    def same_generation(self, other: "Revision") -> bool:
        return (self.uid, self.generation, self.release) == (
            other.uid,
            other.generation,
            other.release,
        )


class ArgoObservation(Model):
    revision: Revision
    ready: bool
    active: bool
    paused: bool
    failed: bool
