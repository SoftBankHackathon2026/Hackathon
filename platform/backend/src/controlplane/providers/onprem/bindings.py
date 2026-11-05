"""Operator-owned bindings. Never accept kubeconfig or arbitrary manifests from users."""

import re

from pydantic import Field, model_validator

from controlplane.domain.models import DeploymentRequest, Model

DNS = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
HOST = r"^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$"


class SecretRef(Model):
    name: str = Field(pattern=DNS)
    key: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    uid: str | None = Field(default=None, min_length=1)


class KubernetesBinding(Model):
    application: str = Field(pattern=DNS, max_length=30)
    environment_id: str = Field(pattern=DNS)
    namespace: str = Field(pattern=DNS)
    ingress_class: str = Field(pattern=DNS)
    hostname: str = Field(pattern=HOST, max_length=253)
    workload_service_account: str = Field(pattern=DNS)
    image_pull_secrets: list[str] = Field(default_factory=list)
    secret_refs: dict[str, SecretRef] = Field(default_factory=dict)
    # These are per-release ceilings, not proof that the VM has spare capacity.
    max_replicas: int = Field(default=1, ge=1, le=5)
    max_cpu: float = Field(default=0.5, gt=0, le=2)
    max_memory_mib: int = Field(default=1024, ge=512, le=4096)

    @model_validator(mode="after")
    def validate_names(self):
        if any(not re.fullmatch(DNS, name) for name in self.image_pull_secrets):
            raise ValueError("Invalid image pull Secret name")
        return self

    def exceeds_ceiling(self, request: DeploymentRequest) -> bool:
        return (
            request.replicas > self.max_replicas
            or float(request.cpu) > self.max_cpu
            or request.memory_mib > self.max_memory_mib
        )
