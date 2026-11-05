from dataclasses import dataclass

from controlplane.domain.models import DeploymentRequest
from oneport_aws.execution.oneport_ecs_app_model import RegisteredApp
from oneport_aws.providers.oneport_ecs.provisioning import ResourceState

MISMATCH = "app_registration_mismatch"


@dataclass(frozen=True)
class Rejection:
    message: str
    code: str


def check_registration(app: RegisteredApp, request: DeploymentRequest) -> Rejection | None:
    resources = request.resources
    if app.database != resources.database:
        return Rejection(
            f"앱 등록의 DB 선택({app.database})과 요청({resources.database})이 다릅니다.", MISMATCH
        )
    if app.wants_database and app.db_state != ResourceState.READY:
        return Rejection(
            f"앱 DB가 아직 준비되지 않았습니다 (db_state={app.db_state}). 준비된 뒤 다시 배포하세요.",
            "db_not_ready",
        )
    if app.redis != resources.redis:
        return Rejection(
            f"앱 등록의 캐시 선택({app.redis})과 요청({resources.redis})이 다릅니다.", MISMATCH
        )
    if app.redis and app.cache_state != ResourceState.READY:
        return Rejection(
            f"앱 캐시가 아직 준비되지 않았습니다 (cache_state={app.cache_state}). 준비된 뒤 다시 배포하세요.",
            "cache_not_ready",
        )
    if (app.port, app.health_path) != (request.port, request.health_path):
        return Rejection(
            "앱 등록 정보(포트·health 경로)와 요청이 다릅니다. register로 갱신하세요.", MISMATCH
        )
    return None
