"""Operator-owned settings and non-secret execution receipts."""

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from controlplane.domain.models import Model
from controlplane.providers.onprem.models import ArgoEnvironment


class ChartArtifact(Model):
    path: Path
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    name: Literal["oneport-app", "oneport-data"] = "oneport-app"
    version: Literal["0.1.0", "0.1.1", "0.2.0", "0.3.0"] = "0.1.0"

    @property
    def supports_instrumentation(self) -> bool:
        return self.version in {"0.2.0", "0.3.0"}

    @property
    def supports_strategy_switch(self) -> bool:
        return self.version == "0.3.0"


class PreviousHelmBinding(Model):
    """Explicit operator-approved predecessor, retaining its immutable rollback package."""

    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    app_chart: ChartArtifact


class HelmEnvironment(ArgoEnvironment):
    namespace: Literal["oneport-apps"] = "oneport-apps"
    cluster_domain: Literal["cluster.local"] = "cluster.local"
    workload_service_account: Literal["oneport-runtime"] = "oneport-runtime"
    hostname: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.yubin\.website$")
    scale_down_delay_seconds: int = Field(default=30, ge=30, le=300)
    progress_deadline_seconds: int = Field(default=600, ge=120, le=1800)
    max_memory_mib: int = Field(default=1024, ge=512, le=2048)

    @field_validator("hostname")
    @classmethod
    def non_system_hostname(cls, value):
        if value in {"kube-api.yubin.website", "origin.yubin.website"}:
            raise ValueError("System hostnames are reserved")
        return value

    # These are reviewed image constraints, never public request overrides.
    run_as_user: int = Field(default=10001, ge=1000, le=65535)
    run_as_group: int = Field(default=10001, ge=1000, le=65535)
    read_only_root_filesystem: bool = False


class HelmPlan(Model):
    release: str = Field(pattern=r"^op-[a-z0-9-]+-[a-f0-9]{10}-(app|data)$", max_length=40)
    namespace: Literal["oneport-apps"] = "oneport-apps"
    chart_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    values: dict
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @property
    def strategy_type(self) -> str:
        return self.values["strategy"]["type"]

    @property
    def route_enabled(self) -> bool:
        return self.values["route"]["enabled"]


class HelmRelease(Model):
    revision: int
    status: str

    def deployed_at(self, revision: int | None) -> bool:
        return self.status == "deployed" and self.revision == revision


def is_deployed_at(release: HelmRelease | None, revision: int | None) -> bool:
    return release is not None and release.deployed_at(revision)


class HelmReceipt(Model):
    release: str
    revision: int
    chart_sha256: str
    values_fingerprint: str
    # Helm accepted a desired state; Argo, health and public verification follow.
    application_verified: Literal[False] = False
