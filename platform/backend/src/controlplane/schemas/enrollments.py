"""마이그레이션 준비 — 환경 연결(등록) API 모델.

등록 스크립트는 사용자의 셸에서 kubectl 이나 aws 명령으로 읽기 전용 접근 권한을 만들고, 원본 조회 결과를
그대로 보낸다(Report). 서버가 그 원본을 검증해 화면용 환경 정보(Inventory)로 정리한다.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from controlplane.domain.models import Model

EnrollmentKind = Literal["kubernetes", "aws"]
EnrollmentStatus = Literal["pending", "registered", "expired"]
Distribution = Literal["k3s", "eks", "gke", "aks", "kubernetes"]


class EnrollmentCreate(Model):
    kind: EnrollmentKind


class KubernetesNode(Model):
    name: str
    control_plane: bool
    cpu_cores: float
    memory_bytes: int
    allocatable_cpu_cores: float
    allocatable_memory_bytes: int
    kubelet_version: str
    os_image: str
    kernel_version: str
    container_runtime: str
    architecture: str
    internal_ip: str | None = None


class StorageClassInfo(Model):
    name: str
    provisioner: str
    default: bool


class KubernetesInventory(Model):
    kind: Literal["kubernetes"] = "kubernetes"
    context: str
    server: str
    version: str
    distribution: Distribution
    managed_control_plane: bool
    node_count: int
    control_plane_count: int
    worker_count: int
    cpu_cores: float
    memory_bytes: int
    nodes: list[KubernetesNode]
    storage_classes: list[StorageClassInfo]
    ingress_classes: list[str]
    namespace_count: int
    controllers: list[str]


class AwsInventory(Model):
    kind: Literal["aws"] = "aws"
    account_id: str
    account_alias: str | None = None
    caller_arn: str
    role_arn: str
    region: str
    eks_clusters: int
    rds_instances: int
    vpcs: int
    msk_clusters: int


Inventory = Annotated[KubernetesInventory | AwsInventory, Field(discriminator="kind")]


class EnrollmentScript(Model):
    path: str
    sha256: str


class EnrollmentView(Model):
    id: str
    kind: EnrollmentKind
    status: EnrollmentStatus
    name: str | None = None
    created_at: datetime
    expires_at: datetime
    registered_at: datetime | None = None
    inventory: Inventory | None = None


class EnrollmentTicket(EnrollmentView):
    """생성 응답에만 일회용 토큰을 담는다. 서버는 토큰의 해시만 저장한다."""

    token: str
    script: EnrollmentScript
    external_id: str | None = None
    aws_principal: str | None = None


class KubernetesReport(Model):
    kind: Literal["kubernetes"]
    context: str = Field(min_length=1, max_length=253)
    server: str = Field(pattern=r"^https://[^\s/]+(/[^\s]*)?$", max_length=512)
    version_json: str = Field(max_length=16_384)
    nodes_tsv: str = Field(max_length=262_144)
    control_plane_nodes: str = Field(default="", max_length=65_536)
    storage_tsv: str = Field(default="", max_length=65_536)
    ingress_classes: str = Field(default="", max_length=16_384)
    namespace_count: int = Field(ge=0, le=100_000)
    deployments: str = Field(default="", max_length=262_144)
    kubeconfig: str = Field(min_length=1, max_length=65_536)


class AwsReport(Model):
    kind: Literal["aws"]
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    account_alias: str | None = Field(default=None, max_length=63)
    caller_arn: str = Field(pattern=r"^arn:aws:(iam|sts)::[0-9]{12}:.+$", max_length=2048)
    role_arn: str = Field(pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$")
    external_id: str = Field(min_length=8, max_length=128)
    region: str = Field(pattern=r"^[a-z]{2}(-gov)?-[a-z]+-[0-9]$")
    eks_clusters: int = Field(ge=0)
    rds_instances: int = Field(ge=0)
    vpcs: int = Field(ge=0)
    msk_clusters: int = Field(ge=0)


Report = Annotated[KubernetesReport | AwsReport, Field(discriminator="kind")]
