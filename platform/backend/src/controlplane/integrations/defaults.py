from controlplane.domain.fleet import (
    ComponentConfig,
    DeclaredFacts,
    EnvironmentConfig,
    FleetConfig,
    MigrationConfig,
)
from controlplane.domain.migration import Direction
from controlplane.schemas.migrations import DataService


def component(id: str, name: str, layer: str, kind: str, **extra: object) -> ComponentConfig:
    return ComponentConfig(id=id, name=name, layer=layer, kind=kind, **extra)


ONPREM_COMPONENTS = [
    component(
        "cloudflared",
        "Cloudflare Tunnel",
        "entry",
        "cloudflared",
        workload="edge-system/deployment/cloudflared",
    ),
    component(
        "traefik",
        "Traefik Ingress",
        "entry",
        "ingress",
        workload="kube-system/deployment/traefik",
        depends_on=["cloudflared"],
    ),
    component(
        "argocd", "Argo CD", "platform", "gitops", workload="argocd/deployment/argocd-server"
    ),
    component(
        "argo-rollouts",
        "Argo Rollouts",
        "platform",
        "rollout",
        workload="argo-rollouts/deployment/argo-rollouts",
    ),
    component(
        "prometheus",
        "Prometheus·Grafana",
        "platform",
        "monitoring",
        workload="monitoring/statefulset/prometheus-monitoring-kube-prometheus-prometheus",
    ),
    component(
        "review-api",
        "review-api",
        "app",
        "app",
        workload="hackathon/deployment/review-api",
        depends_on=["traefik", "postgres", "kafka"],
    ),
    component(
        "review-worker",
        "review-worker",
        "app",
        "app",
        workload="hackathon/deployment/review-worker",
        depends_on=["kafka", "postgres", "qdrant"],
    ),
    component(
        "sample-app", "sample-app", "app", "app", argo_app="sample-app", depends_on=["traefik"]
    ),
    component(
        "db-insert-worker",
        "쓰기 워커",
        "migration",
        "migration",
        argo_app="db-insert-worker",
        depends_on=["postgres"],
    ),
    component(
        "migration-cdc",
        "Kafka Connect (Debezium)",
        "migration",
        "migration",
        argo_app="migration-cdc",
        depends_on=["postgres", "kafka"],
    ),
    component(
        "migration-policy", "OPA 정책 서버", "migration", "migration", argo_app="migration-policy"
    ),
    component(
        "postgres", "PostgreSQL 16", "data", "postgres", workload="hackathon/statefulset/postgres"
    ),
    component(
        "kafka", "Kafka (KRaft)", "data", "kafka", workload="hackathon/statefulset/kafka-broker"
    ),
    component(
        "seaweedfs",
        "SeaweedFS (S3 호환)",
        "data",
        "object_storage",
        workload="hackathon/statefulset/object-storage",
    ),
    component(
        "qdrant",
        "Qdrant",
        "data",
        "vector_db",
        workload="hackathon/statefulset/qdrant",
        placement=["k3s 클러스터", "ns hackathon"],
    ),
    component(
        "tailscale-onprem",
        "Tailscale 경로",
        "link",
        "network",
        note="AWS VPC 10.0.0.0/16 (RDS·MSK)으로 가는 경로",
    ),
]
AWS_COMPONENTS = [
    component(
        "alb",
        "ALB oneaction-alb",
        "entry",
        "load_balancer",
        managed=True,
        note="현재 HTTP 80 임시 모드",
    ),
    component(
        "aws-lbc",
        "AWS Load Balancer Controller",
        "entry",
        "ingress",
        workload="kube-system/deployment/aws-load-balancer-controller",
        depends_on=["alb"],
    ),
    component(
        "argocd", "Argo CD", "platform", "gitops", workload="argocd/deployment/argocd-server"
    ),
    component(
        "eso",
        "External Secrets Operator",
        "platform",
        "secrets",
        workload="external-secrets/deployment/external-secrets",
    ),
    component(
        "cloudwatch", "CloudWatch 로그/알람", "platform", "monitoring", managed=True, placement=[]
    ),
    component(
        "argo-rollouts",
        "Argo Rollouts",
        "platform",
        "rollout",
        workload="argo-rollouts/deployment/argo-rollouts",
    ),
    component(
        "prometheus",
        "Prometheus·Grafana",
        "platform",
        "monitoring",
        workload="monitoring/statefulset/prometheus-monitoring-kube-prometheus-prometheus",
    ),
    component(
        "review-service",
        "review-service",
        "app",
        "app",
        argo_app="review-service-aws",
        depends_on=["aws-lbc", "rds", "msk"],
    ),
    component(
        "sample-app",
        "sample-app",
        "app",
        "app",
        argo_app="sample-app-aws",
        depends_on=["aws-lbc"],
    ),
    component(
        "rds",
        "RDS oneaction-review-db",
        "data",
        "postgres",
        managed=True,
        endpoint_hints=["oneaction-review-db."],
        placement=["VPC 10.0.0.0/16", "data 서브넷"],
    ),
    component(
        "msk",
        "MSK oneaction-review",
        "data",
        "kafka",
        managed=True,
        endpoint_hints=[".kafka.ap-northeast-2.amazonaws.com"],
        placement=["VPC 10.0.0.0/16", "data 서브넷"],
    ),
    component(
        "s3",
        "S3 oneaction-review-docs",
        "data",
        "object_storage",
        managed=True,
        placement=[],
        endpoint_hints=["oneaction-review-docs"],
    ),
    component(
        "tailscale-aws",
        "Tailscale 라우터",
        "link",
        "network",
        managed=True,
        note="oneaction-aws-router, 10.0.0.0/16 광고",
    ),
]

GCP_COMPONENTS = [
    component(
        "cloud-lb",
        "Cloud Load Balancing",
        "entry",
        "load_balancer",
        managed=True,
        note="Ingress platform/oneaction-dev-platform, HTTP 80",
    ),
    component(
        "argocd", "Argo CD", "platform", "gitops", workload="argocd/deployment/argocd-server"
    ),
    component(
        "eso",
        "External Secrets Operator",
        "platform",
        "secrets",
        workload="external-secrets/deployment/external-secrets",
    ),
    component(
        "cloud-logging", "Cloud Logging/알림", "platform", "monitoring", managed=True, placement=[]
    ),
    component(
        "review-service",
        "review-service",
        "app",
        "app",
        argo_app="review-service-gcp",
        depends_on=["cloud-lb", "cloud-sql", "managed-kafka"],
    ),
    component(
        "sample-app", "sample-app", "app", "app", argo_app="sample-app-gcp", depends_on=["cloud-lb"]
    ),
    component(
        "cloud-sql",
        "Cloud SQL oneaction-dev-review",
        "data",
        "postgres",
        managed=True,
        endpoint_hints=[":oneaction-dev-review"],
        placement=["VPC oneaction-dev-vpc", "data 서브넷"],
    ),
    component(
        "managed-kafka",
        "Managed Kafka oneaction-dev-review",
        "data",
        "kafka",
        managed=True,
        endpoint_hints=[".managedkafka."],
        placement=["VPC oneaction-dev-vpc", "data 서브넷"],
    ),
    component(
        "gcs",
        "Cloud Storage oneaction-dev-review",
        "data",
        "object_storage",
        managed=True,
        endpoint_hints=["-oneaction-dev-review"],
        placement=[],
    ),
]

K3S = "k3s 클러스터"
VPC = "VPC 10.0.0.0/16"
EKS_SUBNET = "EKS 서브넷"
EKS = "EKS oneaction"
ONPREM_PLACEMENT = {
    "cloudflared": [K3S, "ns edge-system"],
    "traefik": [K3S, "ns kube-system"],
    "argocd": [K3S, "ns argocd"],
    "argo-rollouts": [K3S, "ns argo-rollouts"],
    "prometheus": [K3S, "ns monitoring"],
}
AWS_PLACEMENT = {
    "alb": [VPC, "public 서브넷"],
    "aws-lbc": [VPC, EKS_SUBNET, EKS, "ns kube-system"],
    "argocd": [VPC, EKS_SUBNET, EKS, "ns argocd"],
    "eso": [VPC, EKS_SUBNET, EKS, "ns external-secrets"],
    "argo-rollouts": [VPC, EKS_SUBNET, EKS, "ns argo-rollouts"],
    "prometheus": [VPC, EKS_SUBNET, EKS, "ns monitoring"],
    "review-service": [VPC, EKS_SUBNET, EKS, "ns platform"],
    "sample-app": [VPC, EKS_SUBNET, EKS, "ns sample-app"],
    "rds": [VPC, "data 서브넷"],
    "msk": [VPC, "data 서브넷"],
    "tailscale-aws": [VPC, EKS_SUBNET],
}
GCP_VPC = "VPC oneaction-dev-vpc"
GKE_SUBNET = "GKE 서브넷"
GKE = "GKE oneaction-dev"
GCP_PLACEMENT = {
    "cloud-lb": [GCP_VPC],
    "argocd": [GCP_VPC, GKE_SUBNET, GKE, "ns argocd"],
    "eso": [GCP_VPC, GKE_SUBNET, GKE, "ns external-secrets"],
    "review-service": [GCP_VPC, GKE_SUBNET, GKE, "ns platform"],
    "sample-app": [GCP_VPC, GKE_SUBNET, GKE, "ns platform"],
    "cloud-sql": [GCP_VPC, "data 서브넷"],
    "managed-kafka": [GCP_VPC, "data 서브넷"],
}


def placed(
    components: list[ComponentConfig], placement: dict[str, list[str]], default: list[str]
) -> list[ComponentConfig]:
    return [
        c.model_copy(
            update={"placement": placement.get(c.id, default if c.workload or c.argo_app else [])}
        )
        for c in components
    ]


ONPREM = EnvironmentConfig(
    id="onprem",
    name="온프렘 k3s (서울)",
    kind="k3s",
    provider="onprem",
    region="seoul-local",
    role="legacy",
    context="hackathon",
    app_namespace="hackathon",
    cluster_path=[K3S],
    data_services=[
        DataService(kind="postgres", name="postgres (PostgreSQL 16)", managed=False),
        DataService(kind="kafka", name="kafka (KRaft 3.9.1)", managed=False),
        DataService(kind="object_storage", name="SeaweedFS (S3 호환)", managed=False),
        DataService(kind="vector_db", name="qdrant", managed=False),
    ],
    components=placed(ONPREM_COMPONENTS, ONPREM_PLACEMENT, [K3S, "ns hackathon"]),
    boundary=["사이트 seoul-local"],
    declared=DeclaredFacts(
        threat_detection=False,
        posture_service=False,
        audit_log=None,
        flow_logs=None,
        db_tls_required=True,
        db_public=False,
        db_encrypted_at_rest=None,
        db_backups=True,
        workload_identity=False,
        ingress_tls=False,
        waf=False,
        policy_gates=True,
    ),
)

AWS = EnvironmentConfig(
    id="aws",
    name="AWS EKS oneaction (서울)",
    kind="eks",
    provider="aws",
    region="ap-northeast-2",
    role="target",
    context="oneaction",
    app_namespace="platform",
    cluster_path=[VPC, EKS_SUBNET, EKS],
    data_services=[
        DataService(kind="postgres", name="RDS oneaction-review-db", managed=True),
        DataService(kind="kafka", name="MSK oneaction-review", managed=True),
        DataService(kind="object_storage", name="S3 oneaction-review-docs", managed=True),
    ],
    components=placed(AWS_COMPONENTS, AWS_PLACEMENT, []),
    boundary=["AWS 계정 t3underline", "리전 ap-northeast-2"],
    declared=DeclaredFacts(
        threat_detection=False,
        posture_service=False,
        audit_log=True,
        flow_logs=True,
        db_tls_required=True,
        db_public=False,
        db_encrypted_at_rest=True,
        db_backups=True,
        workload_identity=True,
        ingress_tls=False,
        waf=False,
        policy_gates=True,
    ),
)

GCP = EnvironmentConfig(
    id="gcp",
    name="GCP GKE oneaction-dev (서울)",
    kind="gke",
    provider="gcp",
    region="asia-northeast3",
    role="target",
    context="oneaction-gke",
    app_namespace="platform",
    cluster_path=[GCP_VPC, GKE_SUBNET, GKE],
    data_services=[
        DataService(kind="postgres", name="Cloud SQL oneaction-dev-review", managed=True),
        DataService(kind="kafka", name="Managed Kafka oneaction-dev-review", managed=True),
        DataService(kind="object_storage", name="Cloud Storage oneaction-dev-review", managed=True),
    ],
    components=placed(GCP_COMPONENTS, GCP_PLACEMENT, []),
    boundary=["GCP 프로젝트 ghost-project", "리전 asia-northeast3"],
    declared=DeclaredFacts(
        threat_detection=False,
        posture_service=False,
        audit_log=True,
        flow_logs=True,
        db_tls_required=True,
        db_public=False,
        db_encrypted_at_rest=True,
        db_backups=True,
        workload_identity=True,
        ingress_tls=False,
        waf=False,
        policy_gates=True,
    ),
)

DEFAULT_FLEET = FleetConfig(
    mode="simulated",
    environments=[ONPREM, AWS, GCP],
    migrations=[
        MigrationConfig(
            job_id="forward_001",
            direction=Direction.FORWARD,
            source_env="onprem",
            target_env="aws",
            data_source="onprem",
            data_target="aws",
            worker_url="http://db-insert-worker:8080",
            connect_url="http://migration-connect:8083",
        ),
        MigrationConfig(
            job_id="return_001",
            direction=Direction.RETURN,
            source_env="aws",
            target_env="onprem",
            data_source="aws",
            data_target="onprem_return",
            worker_url="http://db-insert-worker:8080",
            connect_url="http://migration-connect:8083",
        ),
    ],
)
