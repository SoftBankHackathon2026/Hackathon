from typing import Any

from controlplane.core.errors import (
    OperationNotFound,
    RequestConflict,
    UnsupportedOperation,
)
from controlplane.domain.models import Runtime
from controlplane.providers.interface import DeploymentProvider
from controlplane.schemas.accounts import Identity
from controlplane.schemas.deletions import DeletionView, deletion_view
from controlplane.schemas.environments import AppDeletionRequest
from controlplane.services.deployments import DeploymentService, deletion_info

UNREGISTERED_NAMES = {
    Runtime.ONPREM_HELM: "On-Premises · 연결 대기",
    Runtime.AWS_ECS: "AWS · 담당자 연결 대기",
    Runtime.GCP_CLOUD_RUN: "GCP · 담당자 연결 대기",
}
DELETION_NOT_FOUND = "삭제 기록을 찾을 수 없습니다."


def unregistered_environment(runtime: Runtime, name: str) -> dict[str, Any]:
    return {
        "id": runtime.value.replace("_", "-") + "-unregistered",
        "name": name,
        "runtime": runtime,
        "state": "unregistered",
        "executable": False,
        "connection_verified": False,
    }


class EnvironmentService:
    def __init__(self, deployments: DeploymentService) -> None:
        self._deployments = deployments
        self._registry = deployments.registry

    def _decorate_deletions(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deletions = self._deployments.deletions
        latest = deletions.latest_all() if deletions else {}
        for item in items:
            view = deletion_view(latest.get(item["id"]))
            item["deletion"] = view.model_dump() if view else None
            item["app_deletion"] = deletion_info(self._registry.get(item["id"]))
        return items

    def _placeholders(self) -> list[dict[str, Any]]:
        present = self._registry.runtimes()
        return [
            unregistered_environment(runtime, name)
            for runtime, name in UNREGISTERED_NAMES.items()
            if runtime not in present
            and not (runtime == Runtime.ONPREM_HELM and Runtime.ONPREM_ARGO in present)
        ]

    def overview(self, identity: Identity | None) -> list[dict[str, Any]]:
        registered = self._decorate_deletions(self._registry.environments())
        return self._deployments.visible_environments(registered, identity) + self._placeholders()

    def display_name(self, environment_id: str, provider: DeploymentProvider) -> str:
        catalog = self._registry.catalog
        binding = catalog.binding(environment_id) if catalog else None
        return binding["application_name"] if binding else provider.application

    def deletion(self, environment_id: str, identity: Identity | None) -> DeletionView | None:
        try:
            self._deployments.access.require_owner(environment_id, identity)
            self._deployments.provider(environment_id)
        except (OperationNotFound, RequestConflict):
            raise OperationNotFound(DELETION_NOT_FOUND) from None
        deletions = self._deployments.deletions
        return deletion_view(deletions.latest(environment_id)) if deletions else None

    def _deletion_scope(self, info: dict[str, Any], requested: bool) -> bool:
        delete_data = requested or bool(info.get("data_required"))
        return delete_data and bool(info.get("data"))

    def request_deletion(
        self, environment_id: str, body: AppDeletionRequest, identity: Identity | None
    ) -> DeletionView | None:
        deletions = self._deployments.deletions
        if not deletions:
            raise RequestConflict("플랫폼 DB가 연결되지 않아 앱을 삭제할 수 없습니다.")
        self._deployments.access.require_owner(environment_id, identity)
        provider = self._deployments.provider(environment_id)
        info = deletion_info(provider)
        if not info.get("supported"):
            raise UnsupportedOperation(
                info.get("reason") or "이 환경은 앱 삭제를 지원하지 않습니다."
            )
        if body.confirm != self.display_name(environment_id, provider):
            raise RequestConflict("확인용 앱 이름이 일치하지 않습니다.")
        row = deletions.request(
            application=provider.application,
            environment_id=environment_id,
            runtime=provider.runtime,
            delete_data=self._deletion_scope(info, body.delete_data),
            key=body.idempotency_key,
            team=identity.team if identity else None,
            created_by=identity.email if identity else None,
        )
        return deletion_view(deletions.latest(row["environment_id"]))
