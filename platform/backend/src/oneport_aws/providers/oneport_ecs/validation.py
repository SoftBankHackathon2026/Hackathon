from controlplane.domain.models import DeploymentRequest, ImageSource, Strategy
from controlplane.domain.resources import FARGATE_SIZES, validate_resource_pair
from oneport_aws.providers.oneport_ecs.models import OneportEcsEnvironment
from oneport_aws.providers.oneport_ecs.tracing import instrumentation_options

SUPPORTED_STRATEGIES = frozenset({Strategy.BLUE_GREEN, Strategy.CANARY})


def validate_deployment(env: OneportEcsEnvironment, request: DeploymentRequest) -> None:
    validate_resource_pair(request.cpu, request.memory_mib, FARGATE_SIZES)
    if env.required_database != "none" and request.resources.database != env.required_database:
        raise ValueError("앱 계약에 PostgreSQL이 필수입니다. DB 연결을 선택하세요.")
    if (request.application, request.environment_id) != (env.application, env.environment_id):
        raise ValueError("등록된 앱·환경만 배포할 수 있습니다.")
    _validate_image(env, request)
    if request.strategy not in SUPPORTED_STRATEGIES:
        raise ValueError("AWS는 ECS 네이티브 Blue/Green·Canary만 지원합니다.")
    _validate_rollout(request)
    if (
        request.replicas > env.max_replicas
        or float(request.cpu) > env.max_cpu
        or request.memory_mib > env.max_memory_mib
    ):
        raise ValueError("등록된 환경의 replicas·CPU·메모리 상한을 넘었습니다.")
    if any(b.binding not in env.secret_refs for b in request.secret_bindings):
        raise ValueError("등록되지 않은 Secret binding입니다.")
    if request.instrumentation and request.instrumentation not in instrumentation_options(env):
        raise ValueError("이 환경은 trace 수집이나 자동 계측 이미지가 준비되지 않았습니다.")


def _validate_image(env: OneportEcsEnvironment, request: DeploymentRequest) -> None:
    source = request.source
    if not isinstance(source, ImageSource):
        raise ValueError("빌드를 완료하고 digest가 고정된 이미지로 요청하세요.")
    own_prefix = f"{env.ecr_registry}/{env.repository}@"
    if source.image not in env.public_images and not source.image.startswith(own_prefix):
        raise ValueError(
            f"이미지는 {env.ecr_registry}/{env.repository}@sha256:... 또는 등록된 공개 Docker Hub digest여야 합니다."
        )


def _validate_rollout(request: DeploymentRequest) -> None:
    rollout = request.rollout
    if not rollout:
        return
    if rollout.promotion != "automatic":
        raise ValueError("AWS ECS는 트래픽을 자동으로 전환합니다. 수동 전환은 지원하지 않습니다.")
    if not rollout.auto_rollback:
        raise ValueError("AWS는 알람·circuit breaker 자동 복귀를 끌 수 없습니다.")
