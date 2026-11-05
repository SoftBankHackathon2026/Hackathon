from typing import Any

from pydantic import ConfigDict, model_serializer

from controlplane.demo_deployments.states import TargetBlocker
from controlplane.domain.models import Model

REQUIREMENTS = (
    "public_github_repository",
    "repository_dockerfile",
    "linux_amd64",
    "provider_health_endpoint_200",
    "x_launchpad_release_header",
    "compatible_with_configured_demo_apps",
)
PUBLIC_TARGET_FIELDS = {"runtime", "environment_id", "blockers", "spec"}


class SnapshotModel(Model):
    model_config = ConfigDict(extra="ignore")


class TargetSnapshot(SnapshotModel):
    runtime: str
    environment_id: str | None = None
    blockers: list[TargetBlocker] = []
    spec: dict[str, Any] | None = None
    binding_fingerprint: str | None = None
    last_release_id: str | None = None
    owner_team: str | None = None
    request: dict[str, Any] | None = None

    @model_serializer(mode="wrap")
    def _omit_missing_request(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if data.get("request") is None:
            data.pop("request", None)
        return data

    def public(self) -> dict[str, Any]:
        return self.model_dump(mode="json", include=PUBLIC_TARGET_FIELDS)


class RunSnapshot(SnapshotModel):
    input: dict[str, Any]
    source: dict[str, Any]
    commit: str | None = None
    blockers: list[str] = []
    targets: list[TargetSnapshot] = []
    requirements: list[str] = list(REQUIREMENTS)

    @property
    def application_id(self) -> str | None:
        return self.input.get("application_id")

    @property
    def ready(self) -> bool:
        return not self.blockers and all(not t.blockers for t in self.targets)

    def target(self, runtime: str) -> TargetSnapshot:
        return next(t for t in self.targets if t.runtime == runtime)

    def persisted(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def public(self, preview_id: str, expires_at: Any) -> dict[str, Any]:
        view = self.persisted()
        view["targets"] = [target.public() for target in self.targets]
        return {"id": preview_id, "expires_at": expires_at, **view, "ready": self.ready}
