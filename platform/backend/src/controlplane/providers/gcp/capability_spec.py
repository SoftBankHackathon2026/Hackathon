from controlplane.domain.models import Strategy
from controlplane.domain.resources import CLOUD_RUN_SIZES, resource_options
from controlplane.providers.capabilities import (
    FeatureSupport,
    ProviderCapabilities,
    git_build_support,
)

CONFIGURABLE_FIELDS = [
    "port",
    "health_path",
    "strategy",
    "cpu",
    "memory_mib",
    "replicas",
    "architecture",
    "resources",
    "secret_bindings",
    "env",
    "migration",
    "rollout",
    "instrumentation",
    "tracing_enabled",
]
DB_NOT_REGISTERED = "이 환경에는 DB 생성 설정이 등록되지 않았습니다."


def _optional_feature(enabled, detail, unsupported_detail):
    return FeatureSupport(
        stage="sdk_only" if enabled else "unsupported",
        detail=detail if enabled else unsupported_detail,
    )


def _features(settings):
    data = settings.data_provisioning
    return {
        "git_build": git_build_support(),
        "database": _optional_feature(
            data, "앱 전용 Cloud SQL·Secret 자동 생성 및 재사용", DB_NOT_REGISTERED
        ),
        "redis": _optional_feature(
            settings.cache_provisioning,
            "앱 전용 Memorystore·Secret 생성 및 REDIS_URL·TLS CA 연결",
            "이 환경에는 캐시 생성 설정이 등록되지 않았습니다. 기존 연결은 유지합니다.",
        ),
        "migration": _optional_feature(
            data, "후보 배포 전 Cloud Run Job으로 실행. 실패 시 후보 배포 차단", DB_NOT_REGISTERED
        ),
        "image_deploy": FeatureSupport(
            stage="sdk_only", detail="등록된 기존 Cloud Run 서비스 갱신"
        ),
        "blue_green": FeatureSupport(stage="sdk_only", detail="후보 검증 후 수동·자동 100% 전환"),
        "canary": FeatureSupport(stage="sdk_only", detail="비율·유지 시간·수동/자동 전환 설정"),
        "rollback": FeatureSupport(
            stage="sdk_only", detail="수동 복귀·선택형 연속 헬스체크 실패 복귀"
        ),
    }


def build_capabilities(runtime, settings):
    return ProviderCapabilities(
        runtime=runtime,
        integration="sdk_only",
        source_kinds=["image"],
        configurable_fields=list(CONFIGURABLE_FIELDS),
        strategies=[Strategy.BLUE_GREEN, Strategy.CANARY],
        architectures=["X86_64"],
        **resource_options(CLOUD_RUN_SIZES),
        database_modes=["dedicated"] if settings.data_provisioning else [],
        max_replicas=5,
        scaling_semantics="revision_max_instances",
        features=_features(settings),
        prerequisites=[
            "기존 서비스 UID·허용 이미지 등록",
            "Worker의 후보 run.app 및 운영 HTTPS 접근",
            "X-Launchpad-Release 응답 헤더",
        ],
        limitations=[
            "신규 서비스 생성과 DB·Redis 크기 변경, Redis 삭제는 미지원",
            "지표 오류율 기반 롤백·DB 스키마 자동 롤백·리니어 배포는 미구현",
        ],
    )
