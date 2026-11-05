from typing import Any

from controlplane.domain.models import DeploymentRequest, Runtime, Step
from oneport_aws.execution.oneport_ecs_app_model import RegisteredApp
from oneport_aws.execution.oneport_ecs_runtime import OneportEcsSettings
from oneport_aws.providers.oneport_ecs.models import OneportEcsEnvironment
from oneport_aws.providers.oneport_ecs.provisioning import ResourceState
from oneport_aws.providers.oneport_ecs.tracing import instrumentation_options

PLACEHOLDER_DIGEST = "0" * 64
DEFAULT_PORT = 8080
DEFAULT_HEALTH_PATH = "/health"
DB_WAIT_HINT = "(DB 약 7~10분, 캐시 약 2~7분)"


def not_ready_message(label: str, state: ResourceState) -> str:
    if state.in_progress:
        return (
            f"AWS {label}를 만드는 중입니다({state}). 준비 완료 후 다시 배포하세요{DB_WAIT_HINT}."
        )
    if state == ResourceState.UNCERTAIN:
        return f"AWS {label} 생성 결과를 확인해야 합니다. 운영자 확인 후 배포할 수 있습니다."
    return f"AWS {label} 생성에 실패했습니다({state}). 오류 내용을 확인하세요."


def waiting_message(app: RegisteredApp) -> str:
    waiting = []
    if app.wants_database and app.db_state != ResourceState.READY:
        waiting.append(not_ready_message("DB", app.db_state))
    if app.redis and app.cache_state != ResourceState.READY:
        waiting.append(not_ready_message("캐시", app.cache_state))
    return " ".join(waiting)


def check_request_matches(app: RegisteredApp, request: DeploymentRequest) -> None:
    requested = (
        request.port,
        request.health_path,
        request.resources.database,
        request.resources.redis,
    )
    if requested != (app.port, app.health_path, app.database, app.redis):
        raise ValueError("Request does not match registered app")
    if app.wants_database and request.resources.database_mode != app.database_mode:
        raise ValueError("Request does not match registered DB mode")


def deletion_info(env: OneportEcsEnvironment, app: RegisteredApp | None) -> dict[str, Any]:
    data = []
    if app and app.has_dedicated_database:
        data.append(f"RDS {env.db_instance} (최종 스냅샷을 남김)")
    if app and app.redis:
        data.append(f"ElastiCache {env.cache_name}")
    return {
        "supported": True,
        "removes": [
            f"ECS 서비스 {env.service}와 실행 중인 task",
            "ALB 규칙과 blue/green 대상 그룹",
            "Task Definition (앱·마이그레이션)",
            "롤백 알람과 앱 IAM 역할",
        ],
        "data": data,
        "keeps": ["Secrets Manager 시크릿", "CloudWatch 로그"],
        "recreatable": True,
        "after": "데이터를 남기면 앱 등록도 남아서, 다시 배포하면 같은 주소로 올라갑니다.",
    }


def plan_steps(request: DeploymentRequest) -> list[Step]:
    migration = (
        [
            Step(
                id="migrate",
                title="DB 마이그레이션",
                owner="provider",
                detail="배포 전 일회성 task로 실행합니다. 실패하면 배포하지 않습니다.",
            )
        ]
        if request.migration
        else []
    )
    return [
        *migration,
        Step(
            id="prepare",
            title="앱 리소스·Task Definition 준비",
            owner="provider",
            detail="등록 환경·이미지·DB 상태를 확인합니다.",
        ),
        Step(
            id="deploy",
            title="ECS 배포·트래픽 전환",
            owner="provider",
            detail="ALB health 통과 후 ECS가 전환합니다.",
        ),
        Step(
            id="verify",
            title="운영 응답 확인",
            owner="provider",
            detail="실패 시 bake 기간 내 복구를 시도합니다.",
        ),
    ]


def _registration_view(settings: OneportEcsSettings, app: RegisteredApp | None) -> dict[str, Any]:
    return {
        "registered": bool(app),
        "required_database": settings.required_database,
        "database": app.database if app else "none",
        "database_mode": _database_mode(settings, app),
        "db_state": app.db_state if app else "none",
        "db_error": app.db_error if app else None,
        "redis": app.redis if app else False,
        "cache_state": app.cache_state if app else "none",
        "cache_error": app.cache_error if app else None,
    }


def _database_mode(settings: OneportEcsSettings, app: RegisteredApp | None) -> str:
    if app:
        return app.database_mode
    return "dedicated" if settings.catalog_dynamic else "shared"


def _rollout_template(env: OneportEcsEnvironment) -> dict[str, Any]:
    return {
        "canary_percent": max(1, min(99, round(env.canary_percent))),
        "hold_seconds": env.canary_bake_minutes * 60,
        "promotion": "automatic",
        "auto_rollback": True,
        "failure_threshold": 3,
        "bake_seconds": max(60, min(600, env.bake_minutes * 60)),
    }


def _request_template(
    settings: OneportEcsSettings, env: OneportEcsEnvironment, app: RegisteredApp | None
) -> dict[str, Any]:
    image = next(iter(settings.allowed_images), f"{env.repository_uri}@sha256:{PLACEHOLDER_DIGEST}")
    return {
        "application": settings.application,
        "environment_id": settings.environment_id,
        "source": {"kind": "image", "image": image},
        "strategy": "blue_green",
        "rollout": _rollout_template(env),
        "port": app.port if app else DEFAULT_PORT,
        "health_path": app.health_path if app else DEFAULT_HEALTH_PATH,
        "cpu": "0.25",
        "memory_mib": 512,
        "replicas": 1,
        "architecture": "X86_64",
        "resources": {
            "database": app.database if app else "none",
            "database_mode": _database_mode(settings, app),
            "redis": app.redis if app else False,
        },
        "env": {},
        "secret_bindings": [],
        "migration": None,
    }


def public_environment(
    settings: OneportEcsSettings, env: OneportEcsEnvironment, app: RegisteredApp | None
) -> dict[str, Any]:
    return {
        "id": settings.environment_id,
        "name": f"AWS · {settings.application}",
        "runtime": Runtime.AWS_ECS,
        "application": settings.application,
        "state": "registered",
        "executable": True,
        "connection_verified": False,
        "input_kind": "image",
        "service_access": "public",
        "images": settings.allowed_images,
        "max_replicas": env.max_replicas,
        "max_cpu": env.max_cpu,
        "max_memory_mib": env.max_memory_mib,
        "service_url": env.url,
        "strategy_options": {
            "strategies": ["blue_green", "canary"],
            "canary_percent": env.canary_percent,
            "canary_bake_minutes": env.canary_bake_minutes,
            "bake_minutes": env.bake_minutes,
        },
        "release_restore": True,
        "tracing": env.tracing,
        "instrumentation_options": instrumentation_options(env),
        "application_registration": _registration_view(settings, app),
        "request_template": _request_template(settings, env, app),
    }
