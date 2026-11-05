"""App deletion on AWS, one bounded step per worker tick (infra guide §3 step 10).

requested -> service_draining -> routing -> [db_deleting -> cache_deleting] -> deleted

The ECS service goes first and must be INACTIVE before its target groups can be
deleted. Data (dedicated RDS with a final snapshot, ElastiCache) is removed only
when the user asked for it; otherwise the app registration and its data stay, and
the next deploy recreates the service on the same domain. Secrets and logs are kept.
"""

import time
from collections.abc import Callable
from enum import StrEnum

from controlplane.execution.deletions import Deletion
from oneport_aws.execution.oneport_ecs_app_model import RegisteredApp
from oneport_aws.execution.oneport_ecs_apps import AppRegistry
from oneport_aws.providers.oneport_ecs import cache, database
from oneport_aws.providers.oneport_ecs.provider import OneportEcsProvider

SERVICE_ACTIVE = "ACTIVE"
SERVICE_INACTIVE = "INACTIVE"


class DeletionState(StrEnum):
    REQUESTED = "requested"
    SERVICE_DRAINING = "service_draining"
    ROUTING = "routing"
    DB_DELETING = "db_deleting"
    CACHE_DELETING = "cache_deleting"


def _service_status(provider: OneportEcsProvider) -> str | None:
    env = provider.environment
    return provider.gateways.ecs.get_service_status(env.cluster, env.service)


def _request_service_deletion(deletion: Deletion, provider: OneportEcsProvider, _: AppRegistry):
    env = provider.environment
    if _service_status(provider) == SERVICE_ACTIVE:
        provider.gateways.ecs.delete_service(env.cluster, env.service)
    deletion.move(
        DeletionState.SERVICE_DRAINING, f"ECS 서비스 {env.service}를 삭제하고 task를 멈춥니다."
    )


def _wait_for_drain(deletion: Deletion, provider: OneportEcsProvider, _: AppRegistry):
    if _service_status(provider) not in (None, SERVICE_INACTIVE):
        deletion.wait("ECS 서비스가 task를 정리하는 중입니다.")
        return
    deletion.move(
        DeletionState.ROUTING, "ECS 서비스를 삭제했습니다. ALB 규칙과 나머지 리소스를 지웁니다."
    )


def _delete_routing(deletion: Deletion, provider: OneportEcsProvider, registry: AppRegistry):
    env = provider.environment
    deletion.cp["removed"] = provider.delete_app()
    app = registry.get(env.application, env.environment_id)
    if not deletion.delete_data or app is None:
        deletion.done(_kept(app))
        return
    deletion.move(
        DeletionState.DB_DELETING,
        "ALB 규칙·대상 그룹·Task Definition·알람·앱 IAM 역할을 지웠습니다. 데이터를 지웁니다.",
    )


def _delete_database(deletion: Deletion, provider: OneportEcsProvider, registry: AppRegistry):
    env = provider.environment
    app = registry.get(env.application, env.environment_id)
    if app and app.has_dedicated_database:
        instance = database.describe(provider)
        if instance is not None:
            _drive_database_deletion(deletion, provider, instance.status)
            return
    deletion.move(DeletionState.CACHE_DELETING, "DB 정리를 마쳤습니다.")


def _drive_database_deletion(deletion: Deletion, provider: OneportEcsProvider, status: str):
    env = provider.environment
    if status != database.RdsStatus.DELETING and not deletion.cp.get("snapshot"):
        deletion.cp["snapshot"] = database.delete(provider, suffix=f"-{int(time.time())}")
    message = f"RDS {env.db_instance}를 삭제하는 중입니다."
    if deletion.cp.get("snapshot"):
        message += f" 최종 스냅샷 {deletion.cp['snapshot']}을 남깁니다."
    deletion.wait(message)


def _delete_cache(deletion: Deletion, provider: OneportEcsProvider, registry: AppRegistry):
    env = provider.environment
    app = registry.get(env.application, env.environment_id)
    if app and app.redis:
        finished, message = cache.delete_step(provider)
        if not finished:
            deletion.wait(message)
            return
    registry.unregister(env.application, env.environment_id)
    snapshot = deletion.cp.get("snapshot")
    deletion.done(
        "앱과 데이터를 삭제했습니다. 앱 등록도 지웠습니다. 시크릿과 로그는 남겨 둡니다."
        + (f" RDS 최종 스냅샷: {snapshot}" if snapshot else "")
    )


_HANDLERS: dict[str, Callable] = {
    DeletionState.REQUESTED: _request_service_deletion,
    DeletionState.SERVICE_DRAINING: _wait_for_drain,
    DeletionState.ROUTING: _delete_routing,
    DeletionState.DB_DELETING: _delete_database,
    DeletionState.CACHE_DELETING: _delete_cache,
}


def step(deletion: Deletion, provider: OneportEcsProvider, registry: AppRegistry) -> None:
    handler = _HANDLERS.get(deletion.state)
    if handler is None:
        raise ValueError("Unknown AWS deletion state")
    handler(deletion, provider, registry)


def _kept(app: RegisteredApp | None) -> str:
    if app is None:
        return "앱을 삭제했습니다."
    data = [
        label
        for label, chosen in (("RDS", app.has_dedicated_database), ("ElastiCache", app.redis))
        if chosen
    ]
    return "앱을 삭제했습니다." + (
        f" {'·'.join(data)}와 앱 등록은 남겨 두었습니다. 다시 배포하면 같은 주소로 올라갑니다."
        if data
        else " 앱 등록은 남겨 두었습니다. 다시 배포하면 같은 주소로 올라갑니다."
    )
