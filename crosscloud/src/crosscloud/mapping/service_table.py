"""AWS↔GCP 서비스 대응표(services 표) — 판정의 정본 데이터.

행 하나가 "이 방향에서 이런 원본 자원은 대상의 무엇으로 간다"를 말한다.
equivalent·substitute 판정은 양쪽 클라우드의 공식 문서 인용이 있어야 한다
(프로필 4절 — 공식 문서 근거가 없으면 대응 가능으로 판정하지 않는다).
인용 URL 은 2026-11-07 에 열어 확인했다(_workspace/research/18·19 와 직접 확인).

본선 첫 행만 둔다: ECS Fargate↔Cloud Run · RDS PG↔Cloud SQL PG ·
Secrets Manager↔Secret Manager · ECR↔Artifact Registry · ALB↔외부 Application
LB · Route 53 비공개 영역(원본 유지).
"""

import dataclasses
import datetime

from crosscloud import model


@dataclasses.dataclass(frozen=True)
class Citation:
    """판정 근거 공식 문서 하나."""

    url: str
    checked_on: datetime.date


@dataclasses.dataclass(frozen=True)
class ServiceRow:
    """서비스 대응 한 행.

    Attributes:
        row_id: `<방향>/<원본>-<대상>` 형식의 고유 이름.
        direction: 이 행이 쓰이는 이전 방향.
        source_kind: 원본 자원 종류.
        source_platform: 워크로드 행이면 원본 컴퓨트 플랫폼. 아니면 None.
        required_attributes: 원본 자원 속성이 모두 이 값과 같아야 이 행에
            맞는다(예: engine=postgres).
        target_kind: 대상 자원 종류. retained 면 None.
        target_platform: 워크로드 행이면 대상 컴퓨트 플랫폼.
        verdict: 판정.
        entry_kinds: 워크로드 행이 받는 진입점 종류. 그 밖의 진입점이 있는
            워크로드는 사람 결정이다(계획 편차 E6 — 소진은 HTTP 만).
        citations: 판정 근거 공식 문서.
        notes: 사람이 읽을 비고.
    """

    row_id: str
    direction: model.Direction
    source_kind: model.ResourceKind
    source_platform: model.ComputePlatform | None
    required_attributes: tuple[model.KeyValue, ...]
    target_kind: model.ResourceKind | None
    target_platform: model.ComputePlatform | None
    verdict: model.Verdict
    entry_kinds: tuple[model.EntryKind, ...]
    citations: tuple[Citation, ...]
    notes: str


_CHECKED_ON = datetime.date(2026, 11, 7)
_AWS_DOCS = "https://docs.aws.amazon.com"
_GCP_DOCS = "https://docs.cloud.google.com"
# Google 공식 AWS·Azure·GCP 서비스 비교표. Fargate·RDS·Secrets Manager·ECR·
# Elastic Load Balancing 의 GCP 대응을 한 쪽에서 말한다.
_COMPARISON = f"{_GCP_DOCS}/docs/get-started/aws-azure-gcp-service-comparison"


def _cite(*urls: str) -> tuple[Citation, ...]:
    return tuple(Citation(url=url, checked_on=_CHECKED_ON) for url in urls)


_COMPUTE_CITATIONS = _cite(
    _COMPARISON,
    f"{_AWS_DOCS}/AmazonECS/latest/developerguide/ecs_services.html",
    f"{_AWS_DOCS}/AmazonECS/latest/developerguide/task-iam-roles.html",
    f"{_AWS_DOCS}/AmazonECS/latest/developerguide/"
    "secrets-envvar-secrets-manager.html",
    f"{_GCP_DOCS}/run/docs/securing/service-identity",
    f"{_GCP_DOCS}/run/docs/configuring/services/secrets",
    f"{_GCP_DOCS}/run/docs/securing/ingress",
)
_POSTGRES_CITATIONS = _cite(
    _COMPARISON,
    f"{_GCP_DOCS}/architecture/migrate-aws-rds-aurora-to-postgresql",
    f"{_AWS_DOCS}/AmazonRDS/latest/UserGuide/"
    "PostgreSQL.Concepts.General.FeatureSupport.LogicalReplication.html",
    f"{_GCP_DOCS}/sql/docs/postgres/replication/configure-logical-replication",
)
_SECRET_CITATIONS = _cite(
    _COMPARISON,
    f"{_AWS_DOCS}/secretsmanager/latest/userguide/whats-in-a-secret.html",
    f"{_GCP_DOCS}/secret-manager/docs/overview",
)
_REGISTRY_CITATIONS = _cite(
    _COMPARISON,
    f"{_AWS_DOCS}/AmazonECR/latest/userguide/what-is-ecr.html",
    f"{_GCP_DOCS}/artifact-registry/docs/overview",
)
_LOAD_BALANCER_CITATIONS = _cite(
    _COMPARISON,
    f"{_AWS_DOCS}/elasticloadbalancing/latest/application/introduction.html",
    f"{_GCP_DOCS}/load-balancing/docs/https",
    f"{_GCP_DOCS}/load-balancing/docs/negs/serverless-neg-concepts",
)


def _attributes(**values: str | bool) -> tuple[model.KeyValue, ...]:
    return tuple(model.KeyValue(key=k, value=v) for k, v in values.items())


SERVICE_ROWS: tuple[ServiceRow, ...] = (
    ServiceRow(
        row_id="aws_to_gcp/ecs_fargate-cloud_run",
        direction="aws_to_gcp",
        source_kind="workload",
        source_platform="ecs",
        required_attributes=(),
        target_kind="workload",
        target_platform="cloud_run",
        verdict="substitute",
        entry_kinds=("http",),
        citations=_COMPUTE_CITATIONS,
        notes="작업 역할→서비스 계정, 작업 정의 secrets→Secret Manager 참조."
        " Cloud Run 은 Secret JSON 키 단위 주입이 없어 키별 Secret 으로 나눈다",
    ),
    ServiceRow(
        row_id="gcp_to_aws/cloud_run-ecs_fargate",
        direction="gcp_to_aws",
        source_kind="workload",
        source_platform="cloud_run",
        required_attributes=(),
        target_kind="workload",
        target_platform="ecs",
        verdict="substitute",
        entry_kinds=("http",),
        citations=_COMPUTE_CITATIONS,
        notes="서비스 계정→작업 역할, secretKeyRef→작업 정의 secrets",
    ),
    ServiceRow(
        row_id="aws_to_gcp/rds_postgres-cloud_sql_postgres",
        direction="aws_to_gcp",
        source_kind="postgres_instance",
        source_platform=None,
        required_attributes=_attributes(engine="postgres"),
        target_kind="postgres_instance",
        target_platform=None,
        verdict="equivalent",
        entry_kinds=(),
        citations=_POSTGRES_CITATIONS,
        notes="대상은 cloudsql.logical_decoding=on 으로 만든다",
    ),
    ServiceRow(
        row_id="gcp_to_aws/cloud_sql_postgres-rds_postgres",
        direction="gcp_to_aws",
        source_kind="postgres_instance",
        source_platform=None,
        required_attributes=_attributes(engine="postgres"),
        target_kind="postgres_instance",
        target_platform=None,
        verdict="equivalent",
        entry_kinds=(),
        citations=_POSTGRES_CITATIONS,
        notes="대상은 rds.logical_replication=1 로 만든다",
    ),
    ServiceRow(
        row_id="aws_to_gcp/secrets_manager-secret_manager",
        direction="aws_to_gcp",
        source_kind="secret",
        source_platform=None,
        required_attributes=(),
        target_kind="secret",
        target_platform=None,
        verdict="equivalent",
        entry_kinds=(),
        citations=_SECRET_CITATIONS,
        notes="값 이관은 secret_transfer 단계에서만 한다",
    ),
    ServiceRow(
        row_id="gcp_to_aws/secret_manager-secrets_manager",
        direction="gcp_to_aws",
        source_kind="secret",
        source_platform=None,
        required_attributes=(),
        target_kind="secret",
        target_platform=None,
        verdict="equivalent",
        entry_kinds=(),
        citations=_SECRET_CITATIONS,
        notes="값 이관은 secret_transfer 단계에서만 한다",
    ),
    ServiceRow(
        row_id="aws_to_gcp/ecr-artifact_registry",
        direction="aws_to_gcp",
        source_kind="container_registry",
        source_platform=None,
        required_attributes=(),
        target_kind="container_registry",
        target_platform=None,
        verdict="equivalent",
        entry_kinds=(),
        citations=_REGISTRY_CITATIONS,
        notes="Docker 형식 저장소",
    ),
    ServiceRow(
        row_id="gcp_to_aws/artifact_registry-ecr",
        direction="gcp_to_aws",
        source_kind="container_registry",
        source_platform=None,
        required_attributes=_attributes(format="DOCKER"),
        target_kind="container_registry",
        target_platform=None,
        verdict="equivalent",
        entry_kinds=(),
        citations=_REGISTRY_CITATIONS,
        notes="Docker 형식 저장소만",
    ),
    ServiceRow(
        row_id="aws_to_gcp/alb-external_application_lb",
        direction="aws_to_gcp",
        source_kind="load_balancer",
        source_platform=None,
        required_attributes=_attributes(lb_type="application"),
        target_kind="load_balancer",
        target_platform=None,
        verdict="substitute",
        entry_kinds=(),
        citations=_LOAD_BALANCER_CITATIONS,
        notes="외부 Application LB + 서버리스 NEG 로 Cloud Run 을 묶는다",
    ),
    ServiceRow(
        row_id="gcp_to_aws/external_application_lb-alb",
        direction="gcp_to_aws",
        source_kind="load_balancer",
        source_platform=None,
        required_attributes=_attributes(
            load_balancing_scheme="EXTERNAL_MANAGED"
        ),
        target_kind="load_balancer",
        target_platform=None,
        verdict="substitute",
        entry_kinds=(),
        citations=_LOAD_BALANCER_CITATIONS,
        notes="ALB + 대상 그룹(IP)으로 ECS 서비스를 묶는다",
    ),
    ServiceRow(
        row_id="aws_to_gcp/route53_private-retained",
        direction="aws_to_gcp",
        source_kind="dns_record",
        source_platform=None,
        required_attributes=_attributes(private_zone=True),
        target_kind=None,
        target_platform=None,
        verdict="retained",
        entry_kinds=(),
        citations=_cite(
            f"{_AWS_DOCS}/Route53/latest/DeveloperGuide/"
            "hosted-zones-private.html"
        ),
        notes="사용자 결정(2026-11-07): 비공개 호스팅 영역은 Route 53 에"
        " 남기고 가중치 레코드로 트래픽을 옮긴다",
    ),
)
