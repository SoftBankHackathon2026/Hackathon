"""마이그레이션 준비 1단계 — 환경 연결.

GitHub self-hosted runner 등록처럼, 화면은 정보를 입력받지 않고 실행할 명령만 보여 준다.
1. 화면에서 종류(온프렘 Kubernetes, AWS 계정)를 고르면 일회용 토큰(1시간)이 담긴 명령을 만든다.
2. 사용자가 kubectl·aws 명령이 되는 자기 셸에서 스크립트를 실행한다. 스크립트가 읽기 전용 접근 권한을 만들고
   원본 조회 결과를 이 서버로 보낸다.
3. 서버는 토큰을 확인하고 원본을 검증해 환경 정보로 정리한다. 토큰은 한 번만 쓸 수 있다.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from typing import Any

from controlplane.core.errors import (
    AccessDenied,
    InvalidRequest,
    NotConfigured,
    OperationNotFound,
    RequestConflict,
    Unauthenticated,
)
from controlplane.repositories.enrollments import EnrollmentStore
from controlplane.schemas.accounts import Identity
from controlplane.schemas.enrollments import (
    AwsInventory,
    AwsReport,
    EnrollmentKind,
    EnrollmentScript,
    EnrollmentTicket,
    EnrollmentView,
    KubernetesInventory,
    KubernetesNode,
    KubernetesReport,
    StorageClassInfo,
)

TOKEN_TTL = timedelta(hours=1)
SCRIPT_PACKAGE = "enrollment_scripts"
SCRIPTS = {"kubernetes": "oneport-enroll-k8s.sh", "aws": "oneport-enroll-aws.sh"}
AWS_PRINCIPAL = re.compile(
    r"^arn:aws:(iam|sts)::[0-9]{12}:(role|user|assumed-role)/[A-Za-z0-9+=,.@_/-]+$"
)
NODE_COLUMNS = 11

# 배포 이름으로 클러스터에 깔린 도구를 알아본다. 마이그레이션 계획(무엇을 옮길지, 대상에 무엇이 있어야 할지)에 쓴다.
CONTROLLERS = (
    ("argocd-server", "Argo CD"),
    ("argo-rollouts", "Argo Rollouts"),
    ("external-secrets", "External Secrets Operator"),
    ("cert-manager", "cert-manager"),
    ("aws-load-balancer-controller", "AWS Load Balancer Controller"),
    ("traefik", "Traefik"),
    ("ingress-nginx-controller", "ingress-nginx"),
    ("kube-prometheus-stack-operator", "Prometheus Operator"),
    ("monitoring-kube-prometheus-operator", "Prometheus Operator"),
    ("prometheus-operator", "Prometheus Operator"),
    ("cloudflared", "Cloudflare Tunnel"),
    ("kyverno", "Kyverno"),
    ("gatekeeper-controller-manager", "OPA Gatekeeper"),
)
BINARY_SUFFIX = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
DECIMAL_SUFFIX = {"k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def script_text(kind: EnrollmentKind) -> str:
    return files("controlplane").joinpath(SCRIPT_PACKAGE, SCRIPTS[kind]).read_text()


def script_info(kind: EnrollmentKind) -> EnrollmentScript:
    digest = hashlib.sha256(script_text(kind).encode()).hexdigest()
    return EnrollmentScript(path=f"/api/v1/enrollments/scripts/{SCRIPTS[kind]}", sha256=digest)


def aws_principal_from_environment() -> str | None:
    value = os.getenv("ONEPORT_AWS_PRINCIPAL_ARN", "").strip()
    if not value:
        return None
    if not AWS_PRINCIPAL.fullmatch(value):
        raise ValueError("ONEPORT_AWS_PRINCIPAL_ARN must be an IAM role or user ARN")
    return value


def cpu_cores(value: str) -> float:
    value = value.strip()
    try:
        if value.endswith("m"):
            return float(Decimal(value[:-1]) / 1000)
        return float(Decimal(value))
    except InvalidOperation:
        raise InvalidRequest(f"CPU 값을 읽지 못했습니다: {value[:32]}") from None


def memory_bytes(value: str) -> int:
    value = value.strip()
    try:
        for suffix, factor in (*BINARY_SUFFIX.items(), *DECIMAL_SUFFIX.items()):
            if value.endswith(suffix):
                return int(Decimal(value[: -len(suffix)]) * factor)
        return int(Decimal(value))
    except InvalidOperation:
        raise InvalidRequest(f"메모리 값을 읽지 못했습니다: {value[:32]}") from None


def distribution_of(version: str, nodes: list[KubernetesNode]) -> str:
    text = " ".join([version, *(n.kubelet_version for n in nodes)])
    if "+k3s" in text:
        return "k3s"
    if "-eks-" in text:
        return "eks"
    if "-gke." in text:
        return "gke"
    if any("aks" in n.name for n in nodes):
        return "aks"
    return "kubernetes"


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def parse_nodes(report: KubernetesReport) -> list[KubernetesNode]:
    control = {line.strip().removeprefix("node/") for line in _lines(report.control_plane_nodes)}
    nodes = []
    for line in _lines(report.nodes_tsv):
        cols = line.split("|")
        if len(cols) != NODE_COLUMNS:
            raise InvalidRequest(
                "노드 정보 형식이 맞지 않습니다. 플랫폼에서 받은 스크립트를 쓰세요."
            )
        name, cpu, memory, a_cpu, a_memory, kubelet, os_image, kernel, runtime, arch, ip = cols
        nodes.append(
            KubernetesNode(
                name=name,
                control_plane=name in control,
                cpu_cores=cpu_cores(cpu),
                memory_bytes=memory_bytes(memory),
                allocatable_cpu_cores=cpu_cores(a_cpu),
                allocatable_memory_bytes=memory_bytes(a_memory),
                kubelet_version=kubelet,
                os_image=os_image,
                kernel_version=kernel,
                container_runtime=runtime,
                architecture=arch,
                internal_ip=ip or None,
            )
        )
    if not nodes:
        raise InvalidRequest("노드를 하나도 읽지 못했습니다.")
    return nodes


def parse_storage(text: str) -> list[StorageClassInfo]:
    classes = []
    for line in _lines(text):
        cols = line.split("|")
        if len(cols) != 3:
            raise InvalidRequest("StorageClass 정보 형식이 맞지 않습니다.")
        name, provisioner, default = cols
        classes.append(
            StorageClassInfo(name=name, provisioner=provisioner, default=default.strip() == "true")
        )
    return classes


def detect_controllers(deployments: str) -> list[str]:
    names = {line.strip().split("/")[-1] for line in _lines(deployments)}
    found: list[str] = []
    for deployment, label in CONTROLLERS:
        if deployment in names and label not in found:
            found.append(label)
    return found


def kubernetes_inventory(report: KubernetesReport) -> KubernetesInventory:
    try:
        version = json.loads(report.version_json)["serverVersion"]["gitVersion"]
    except (ValueError, KeyError, TypeError):
        raise InvalidRequest("Kubernetes 버전 정보를 읽지 못했습니다.") from None
    nodes = parse_nodes(report)
    control = sum(1 for n in nodes if n.control_plane)
    distribution = distribution_of(version, nodes)
    return KubernetesInventory(
        context=report.context,
        server=report.server,
        version=version,
        distribution=distribution,
        managed_control_plane=control == 0 and distribution in ("eks", "gke", "aks"),
        node_count=len(nodes),
        control_plane_count=control,
        worker_count=len(nodes) - control,
        cpu_cores=sum(n.cpu_cores for n in nodes),
        memory_bytes=sum(n.memory_bytes for n in nodes),
        nodes=nodes,
        storage_classes=parse_storage(report.storage_tsv),
        ingress_classes=_lines(report.ingress_classes),
        namespace_count=report.namespace_count,
        controllers=detect_controllers(report.deployments),
    )


def aws_inventory(report: AwsReport) -> AwsInventory:
    if report.role_arn.split(":")[4] != report.account_id:
        raise InvalidRequest("읽기 역할이 보고한 계정에 있지 않습니다.")
    return AwsInventory(**report.model_dump(exclude={"kind", "external_id"}))


def view_of(row: dict[str, Any], now: datetime | None = None) -> EnrollmentView:
    now = now or datetime.now(UTC)
    status = row["status"]
    if status == "pending" and row["expires_at"] <= now:
        status = "expired"
    return EnrollmentView(
        id=row["id"],
        kind=row["kind"],
        status=status,
        name=row["name"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        registered_at=row["registered_at"],
        inventory=row["inventory"],
    )


class EnrollmentService:
    def __init__(self, store: EnrollmentStore, aws_principal: str | None = None) -> None:
        self._store = store
        self._aws_principal = aws_principal

    def create(self, kind: EnrollmentKind, identity: Identity | None) -> EnrollmentTicket:
        external_id = None
        if kind == "aws":
            if self._aws_principal is None:
                raise NotConfigured(
                    "플랫폼의 AWS 주체(ONEPORT_AWS_PRINCIPAL_ARN)가 설정되지 않아 AWS 계정을 연결할 수 없습니다."
                )
            external_id = "oneport-" + secrets.token_hex(12)
        token = secrets.token_urlsafe(32)
        row = self._store.create(
            "env_" + secrets.token_hex(6),
            kind,
            token_hash(token),
            datetime.now(UTC) + TOKEN_TTL,
            team=identity.team if identity else None,
            external_id=external_id,
        )
        return EnrollmentTicket(
            **view_of(row).model_dump(),
            token=token,
            script=script_info(kind),
            external_id=external_id,
            aws_principal=self._aws_principal if kind == "aws" else None,
        )

    def _visible(self, enrollment_id: str, identity: Identity | None) -> dict[str, Any]:
        row = self._store.get(enrollment_id)
        if row is None or row["team"] != (identity.team if identity else None):
            raise OperationNotFound("등록 기록을 찾지 못했습니다.")
        return row

    def get(self, enrollment_id: str, identity: Identity | None) -> EnrollmentView:
        return view_of(self._visible(enrollment_id, identity))

    def list(self, identity: Identity | None) -> list[EnrollmentView]:
        now = datetime.now(UTC)
        return [view_of(row, now) for row in self._store.list(identity.team if identity else None)]

    def complete(self, enrollment_id: str, authorization: str, report: Any) -> EnrollmentView:
        secret = self._store.secret_fields(enrollment_id)
        token = authorization.removeprefix("Bearer ").strip()
        if (
            secret is None
            or not token
            or not hmac.compare_digest(secret["token_hash"], token_hash(token))
        ):
            raise Unauthenticated("등록 토큰이 맞지 않습니다. 화면에서 명령을 새로 만드세요.")
        if secret["status"] != "pending":
            raise RequestConflict(
                "이미 등록을 마친 명령입니다. 다시 연결하려면 명령을 새로 만드세요."
            )
        if secret["expires_at"] <= datetime.now(UTC):
            raise AccessDenied("등록 명령이 만료됐습니다. 화면에서 명령을 새로 만드세요.")
        if report.kind != secret["kind"]:
            raise InvalidRequest("이 명령과 다른 종류의 스크립트에서 보낸 결과입니다.")
        if isinstance(report, KubernetesReport):
            inventory = kubernetes_inventory(report)
            name = report.context
            access = {"kubeconfig": report.kubeconfig, "context": report.context}
        else:
            if not hmac.compare_digest(report.external_id, secret["external_id"] or ""):
                raise InvalidRequest("읽기 역할의 External ID 가 이 명령과 다릅니다.")
            inventory = aws_inventory(report)
            name = report.account_alias or report.account_id
            access = {"role_arn": report.role_arn, "external_id": report.external_id}
        row = self._store.complete(enrollment_id, name, inventory.model_dump(mode="json"), access)
        if row is None:
            raise RequestConflict("등록을 마치지 못했습니다. 화면에서 명령을 새로 만드세요.")
        return view_of(row)
