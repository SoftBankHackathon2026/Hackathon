"""Support discovery is independent of whether an execution adapter is registered."""

from typing import Literal

from pydantic import Field

from controlplane.domain.models import DeploymentRequest, Model, Runtime, Strategy
from controlplane.domain.resources import FARGATE_SIZES, KUBERNETES_SIZES, resource_options


class FeatureSupport(Model):
    stage: Literal["planned", "sdk_only", "unsupported"]
    detail: str


def git_build_support() -> FeatureSupport:
    return FeatureSupport(
        stage="sdk_only",
        detail="웹에서 공개 GitHub 저장소·브랜치·빌드 경로를 지정해 이미지를 빌드하고 배포합니다. "
        "허용 저장소 등록과 공통 CodeBuild Worker 설정이 필요하며 linux/amd64를 지원합니다.",
    )


class ProviderCapabilities(Model):
    runtime: Runtime
    contract_version: str = "2026-10-04.oneport.3"
    source_kinds: list[Literal["image", "git", "repository"]] = Field(default_factory=list)
    configurable_fields: list[str] = Field(default_factory=list)
    integration: Literal["sdk_only", "spec_only"]
    strategies: list[Strategy] = Field(default_factory=list)
    architectures: list[str] = Field(default_factory=list)
    cpu_options: list[str] = Field(default_factory=list)
    memory_options_mib: list[int] = Field(default_factory=list)
    memory_min_by_cpu: dict[str, int] = Field(default_factory=dict)
    memory_options_by_cpu: dict[str, list[int]] = Field(default_factory=dict)
    database_modes: list[Literal["shared", "dedicated", "container"]] = Field(default_factory=list)
    max_replicas: int = 0
    scaling_semantics: str = "not_registered"
    features: dict[str, FeatureSupport] = Field(default_factory=dict)
    prerequisites: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


def provider_capabilities(runtime: Runtime) -> ProviderCapabilities:
    if runtime == Runtime.ONPREM_HELM:
        caps = provider_capabilities(Runtime.ONPREM_ARGO).model_copy(deep=True)
        caps.runtime = Runtime.ONPREM_HELM
        # The reviewed Helm chart accepts at most 2 GiB per app container.
        for field, value in resource_options(
            {cpu: [m for m in values if m <= 2048] for cpu, values in KUBERNETES_SIZES.items()}
        ).items():
            setattr(caps, field, value)
        caps.strategies = [Strategy.BLUE_GREEN, Strategy.CANARY]
        caps.configurable_fields.append("rollout")
        caps.features["canary"] = FeatureSupport(
            stage="sdk_only",
            detail="Helm·Argo 자동 단계 검사 및 Traefik 가중치 전환; 수동 승인 미연결",
        )
        caps.database_modes = ["container"]
        caps.configurable_fields.extend(["instrumentation", "tracing_enabled"])
        caps.features["database"] = FeatureSupport(
            stage="sdk_only",
            detail="Helm 별도 데이터 릴리스: PostgreSQL StatefulSet·PVC, Secret 자동 연결",
        )
        caps.features["redis"] = FeatureSupport(
            stage="sdk_only", detail="Helm 앱 전용 Redis 캐시 및 Secret 연결"
        )
        caps.prerequisites = [
            "Helm 3.22.0·검증된 앱/데이터 차트 패키지",
            "제한된 kubeconfig·StatefulSet/PVC/Job 권한",
            "이미지 digest·amd64·non-root",
            "공개 HTTPS 및 릴리즈 헤더",
        ]
        caps.limitations = [
            "자동 Blue-Green·Canary 연결; 차트 0.3.0부터 기존 버전으로 전략 변경 준비",
            "생성 후 데이터 설정 변경·수동 승인 미지원",
            "앱 실패 시 DB 보존; PostgreSQL HA·백업은 별도",
        ]
        return caps

    if runtime == Runtime.AWS_ECS:
        return ProviderCapabilities(
            runtime=runtime,
            integration="sdk_only",
            source_kinds=["image"],
            configurable_fields=[
                "port",
                "health_path",
                "strategy",
                "cpu",
                "memory_mib",
                "replicas",
                "architecture",
                "env",
                "secret_bindings",
                "resources",
                "rollout",
                "migration",
                "instrumentation",
                "tracing_enabled",
            ],
            strategies=[Strategy.BLUE_GREEN, Strategy.CANARY],
            architectures=["X86_64", "ARM64"],
            **resource_options(FARGATE_SIZES),
            database_modes=["shared", "dedicated"],
            max_replicas=5,
            scaling_semantics="fixed_replicas",
            features={
                "app_registration": FeatureSupport(
                    stage="sdk_only", detail="앱 포트·health 경로·DB·캐시 선택 등록"
                ),
                "image_deploy": FeatureSupport(
                    stage="sdk_only",
                    detail="ECS Fargate 이미지 배포·Worker 실행·공통 배포 이력 연결",
                ),
                "database": FeatureSupport(
                    stage="sdk_only",
                    detail="Shared: 사전 등록 Secret 연결. Dedicated: 앱별 RDS PostgreSQL 자동 생성"
                    " 후 DATABASE_URL 주입",
                ),
                "redis": FeatureSupport(
                    stage="sdk_only",
                    detail="앱별 ElastiCache Serverless(Valkey) 자동 생성 후 REDIS_URL 주입",
                ),
                "migration": FeatureSupport(
                    stage="sdk_only",
                    detail="배포 전 oneport-<앱>-migration 일회성 task 실행. 실패하면 배포 중단·운영 유지",
                ),
                "git_build": git_build_support(),
            },
            prerequisites=["AWS 운영자 환경 설정·인증", "앱 등록", "플랫폼 DB와 AWS Worker"],
            limitations=[
                "ECS가 트래픽 전환. 전환 전 수동 후보 검증 없음",
                "Dedicated DB(약 7~10분)·캐시(약 2분)는 등록 후 백그라운드로 준비. 준비 전 배포 거절",
            ],
        )
    if runtime == Runtime.GCP_CLOUD_RUN:
        return ProviderCapabilities(
            runtime=runtime,
            integration="sdk_only",
            source_kinds=["repository"],
            scaling_semantics="preserve_existing",
            features={
                "git_build": FeatureSupport(
                    stage="sdk_only",
                    detail="별도 GCP API: 공개 GitHub 기본 브랜치 → 루트 Dockerfile 빌드 → 기존 demo 갱신",
                ),
                "image_deploy": FeatureSupport(
                    stage="unsupported", detail="현재 GCP 접수 API는 저장소 URL만 지원"
                ),
                "database": FeatureSupport(
                    stage="unsupported",
                    detail="기존 서비스 DB/Secret 설정 유지. 신규 생성·변경 없음",
                ),
                "redis": FeatureSupport(stage="unsupported", detail="신규 생성·변경 없음"),
                "migration": FeatureSupport(
                    stage="unsupported", detail="별도 Migration 실행 기능 없음"
                ),
                "blue_green": FeatureSupport(
                    stage="unsupported", detail="웹 API에서 전략을 선택하지 않음. 기존 서비스 갱신"
                ),
            },
            prerequisites=[
                "별도 GCP FastAPI 서비스(로컬 전용)",
                "GCP 서비스 측 ADC·Docker·기존 demo 서비스",
                "플랫폼 DB와 GCP bridge Worker",
            ],
            limitations=[
                "GCP 서비스 재시작 시 메모리 이력 유실. 공통 Worker는 uncertain으로 차단",
                "자원·환경변수·Secret은 기존 설정 유지. 공개 접속을 보장하지 않음",
            ],
        )
    return ProviderCapabilities(
        runtime=runtime,
        integration="sdk_only",
        source_kinds=["image"],
        configurable_fields=[
            "port",
            "health_path",
            "strategy",
            "cpu",
            "memory_mib",
            "replicas",
            "architecture",
            "env",
            "secret_bindings",
            "resources",
            "migration",
        ],
        strategies=[Strategy.BLUE_GREEN],
        architectures=["X86_64"],
        **resource_options(KUBERNETES_SIZES),
        database_modes=["shared"],
        max_replicas=5,
        scaling_semantics="fixed_replicas",
        features={
            "image_deploy": FeatureSupport(
                stage="sdk_only", detail="고정 digest 이미지 → Argo Rollout"
            ),
            "blue_green": FeatureSupport(
                stage="sdk_only", detail="비공개 preview 검증 → Argo 승격 → 운영 HTTPS 검증"
            ),
            "migration": FeatureSupport(
                stage="sdk_only", detail="Job 완료 확인 후 후보 생성. DB 자동 롤백 없음"
            ),
            "rollback": FeatureSupport(
                stage="sdk_only", detail="실패 시 이전 설정 복구. 과거 릴리즈 선택 실행은 후속"
            ),
            "database": FeatureSupport(
                stage="sdk_only", detail="기존 Secret 참조로 PostgreSQL 연결. 새 DB 발급은 미지원"
            ),
            "redis": FeatureSupport(
                stage="sdk_only", detail="기존 Secret 참조로 Redis 연결. 새 캐시 발급은 미지원"
            ),
            "git_build": git_build_support(),
        },
        prerequisites=[
            "등록된 플랫폼 kubeconfig·최소 권한",
            "Argo Rollouts v1.10 · amd64 노드",
            "Worker에서 private preview DNS 접근",
            "Traefik·Cloudflare 공개 HTTPS",
            "검증용 X-Launchpad-Release 응답 헤더",
        ],
        limitations=[
            "등록 환경별 자원·이미지 제한 적용",
            "DB·캐시 자동 발급·HPA·카나리 미지원",
        ],
    )


def validate_provider_contract(request: DeploymentRequest, runtime: Runtime):
    c = provider_capabilities(runtime)
    if request.strategy not in c.strategies or request.architecture not in c.architectures:
        raise ValueError("이 Provider에 등록되지 않은 전략 또는 CPU 아키텍처입니다.")
    if (
        request.cpu not in c.cpu_options
        or request.memory_mib not in c.memory_options_by_cpu.get(request.cpu, [])
        or request.replicas > c.max_replicas
    ):
        raise ValueError("Provider 자원 지원 범위를 확인하세요.")
