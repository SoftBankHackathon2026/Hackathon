import re
from collections.abc import Callable
from dataclasses import dataclass

from controlplane.domain.fleet import EnvironmentSnapshot, Workload
from controlplane.schemas.migrations import ComponentView
from controlplane.schemas.security import Category, ControlResult, Severity, Standard

STANDARDS = [
    Standard(id="CIS-K8S", name="CIS Kubernetes Benchmark v1.10"),
    Standard(id="AWS-FSBP", name="AWS Foundational Security Best Practices"),
    Standard(id="CIS-GCP", name="CIS Google Cloud Platform Foundation Benchmark"),
    Standard(id="NSA-K8S", name="NSA/CISA Kubernetes Hardening Guide"),
    Standard(id="ONEPORT", name="Oneport 마이그레이션 정책 (OPA)"),
]
DIGEST_IMAGE = re.compile(r"@sha256:[0-9a-f]{64}$")

Check = Callable[[EnvironmentSnapshot], ControlResult]


@dataclass(frozen=True)
class Control:
    id: str
    title: str
    category: Category
    severity: Severity
    standard_refs: tuple[str, ...]
    remediation: str
    check: Check
    why: str = ""
    targets: tuple[str, ...] = ()


def passed(detail: str) -> ControlResult:
    return ControlResult(status="pass", detail=detail)


def failed(detail: str) -> ControlResult:
    return ControlResult(status="fail", detail=detail)


def unknown(detail: str = "근거를 수집하지 못했어요.") -> ControlResult:
    return ControlResult(status="unknown", detail=detail)


def declared(value: bool | None, yes: str, no: str) -> ControlResult:
    if value is None:
        return unknown("인프라 설정에서 확인할 수 없어요.")
    return passed(yes) if value else failed(no)


def _app_workloads(snapshot: EnvironmentSnapshot) -> list[Workload]:
    namespace = snapshot.config.app_namespace
    return [w for w in snapshot.cluster.workloads if w.namespace == namespace]


def _workload_rule(attribute: str, label: str) -> Check:
    def check(snapshot: EnvironmentSnapshot) -> ControlResult:
        workloads = _app_workloads(snapshot)
        if not snapshot.cluster.connected or not workloads:
            return unknown()
        offenders = [w.name for w in workloads if not getattr(w, attribute)]
        if offenders:
            return failed(f"{label} 미적용: {', '.join(sorted(offenders))}")
        return passed(f"앱 워크로드 {len(workloads)}개 모두 {label} 적용")

    return check


def _no_privileged(snapshot: EnvironmentSnapshot) -> ControlResult:
    workloads = _app_workloads(snapshot)
    if not snapshot.cluster.connected or not workloads:
        return unknown()
    offenders = sorted(w.name for w in workloads if w.privileged)
    return (
        failed(f"privileged 컨테이너: {', '.join(offenders)}")
        if offenders
        else passed("privileged 컨테이너 없음")
    )


def _digest_pinned(snapshot: EnvironmentSnapshot) -> ControlResult:
    workloads = _app_workloads(snapshot)
    if not snapshot.cluster.connected or not workloads:
        return unknown()
    images = sorted({image for w in workloads for image in w.images})
    loose = [image for image in images if not DIGEST_IMAGE.search(image)]
    if loose:
        return failed(f"digest 미고정 이미지 {len(loose)}/{len(images)}개")
    return passed(f"이미지 {len(images)}개 모두 digest 고정")


def _namespace_observed(snapshot: EnvironmentSnapshot) -> bool:
    cluster = snapshot.cluster
    return cluster.connected and snapshot.config.app_namespace in cluster.namespace_labels


def _default_deny(snapshot: EnvironmentSnapshot) -> ControlResult:
    cluster = snapshot.cluster
    namespace = snapshot.config.app_namespace
    if not _namespace_observed(snapshot):
        return unknown()
    if namespace in cluster.default_deny_namespaces:
        count = cluster.network_policy_count.get(namespace, 0)
        return passed(f"{namespace}: default-deny 포함 NetworkPolicy {count}개")
    return failed(f"{namespace}: default-deny NetworkPolicy 없음")


def _pod_security_admission(snapshot: EnvironmentSnapshot) -> ControlResult:
    cluster = snapshot.cluster
    namespace = snapshot.config.app_namespace
    if not _namespace_observed(snapshot):
        return unknown()
    level = cluster.namespace_labels[namespace].get("pod-security.kubernetes.io/enforce")
    if level in ("restricted", "baseline"):
        return passed(f"{namespace}: PSA enforce={level}")
    return failed(f"{namespace}: Pod Security Admission enforce 라벨 없음")


def _rbac_wildcards(snapshot: EnvironmentSnapshot) -> ControlResult:
    cluster = snapshot.cluster
    if not cluster.connected or cluster.wildcard_bindings is None:
        return unknown()
    if cluster.wildcard_bindings:
        return failed(f"와일드카드 권한 바인딩: {', '.join(sorted(cluster.wildcard_bindings))}")
    return passed("와일드카드(*) 권한 바인딩 없음")


def _argocd_hardening(snapshot: EnvironmentSnapshot) -> ControlResult:
    cluster = snapshot.cluster
    if cluster.argocd_admin_enabled is None or cluster.argocd_anonymous_enabled is None:
        return unknown("Argo CD 설정을 읽지 못했어요.")
    if cluster.argocd_admin_enabled or cluster.argocd_anonymous_enabled:
        return failed("Argo CD admin 또는 익명 접근이 켜져 있음")
    return passed("Argo CD admin·익명 접근 비활성")


def _external_secrets(snapshot: EnvironmentSnapshot) -> ControlResult:
    count = snapshot.cluster.external_secrets
    if count is None:
        return unknown()
    if count:
        return passed(f"ExternalSecret {count}개로 비밀값 동기화")
    return failed("Secret을 스크립트로 직접 생성 (외부 비밀 저장소 미사용)")


def _resource_quota(snapshot: EnvironmentSnapshot) -> ControlResult:
    cluster = snapshot.cluster
    namespace = snapshot.config.app_namespace
    if not _namespace_observed(snapshot):
        return unknown()
    if namespace in cluster.resource_quota_namespaces:
        return passed(f"{namespace}: ResourceQuota 적용")
    return failed(f"{namespace}: ResourceQuota 없음")


def _declared(attribute: str, yes: str, no: str) -> Check:
    def check(snapshot: EnvironmentSnapshot) -> ControlResult:
        return declared(getattr(snapshot.config.declared, attribute), yes, no)

    return check


def _db_private(snapshot: EnvironmentSnapshot) -> ControlResult:
    public = snapshot.config.declared.db_public
    if public is None:
        return unknown("인프라 설정에서 확인할 수 없어요.")
    return (
        failed("DB가 공개 엔드포인트로 노출됨") if public else passed("DB가 사설망에서만 접근 가능")
    )


CONTROLS: tuple[Control, ...] = (
    Control(
        "NET-01",
        "앱 네임스페이스 default-deny NetworkPolicy",
        "network",
        "high",
        ("CIS-K8S 5.3.2", "NSA-K8S"),
        "앱 네임스페이스에 ingress default-deny 정책을 두고 필요한 흐름만 허용하세요.",
        _default_deny,
    ),
    Control(
        "NET-02",
        "DB 비공개 (SEC-01)",
        "network",
        "critical",
        ("ONEPORT SEC-01", "AWS-FSBP RDS.2", "CIS-GCP 6.6"),
        "DB를 사설 서브넷·ClusterIP로만 노출하세요. Cloud SQL은 공인 IP를 끄세요.",
        _db_private,
    ),
    Control(
        "NET-03",
        "외부 진입점 TLS",
        "network",
        "high",
        ("AWS-FSBP ELB.1",),
        "ALB·Cloud Load Balancing·Ingress에 HTTPS 리스너와 인증서를 연결하세요.",
        _declared("ingress_tls", "외부 진입점이 TLS로 보호됨", "외부 진입점이 평문 HTTP"),
    ),
    Control(
        "NET-04",
        "웹 방화벽(WAF)",
        "network",
        "medium",
        ("AWS-FSBP WAF.1",),
        "ALB 앞에 AWS WAF, Cloud Load Balancing 앞에 Cloud Armor(또는 오픈소스 WAF Ingress)를 두세요.",
        _declared("waf", "WAF 적용", "WAF 미적용"),
    ),
    Control(
        "WL-01",
        "컨테이너 비루트 실행",
        "workload",
        "high",
        ("CIS-K8S 5.2.6", "NSA-K8S"),
        "securityContext.runAsNonRoot=true를 지정하세요.",
        _workload_rule("run_as_non_root", "runAsNonRoot"),
    ),
    Control(
        "WL-02",
        "읽기 전용 루트 파일시스템",
        "workload",
        "medium",
        ("NSA-K8S",),
        "readOnlyRootFilesystem=true와 쓰기 볼륨 분리를 적용하세요.",
        _workload_rule("read_only_root", "readOnlyRootFilesystem"),
    ),
    Control(
        "WL-03",
        "Linux capability 전체 제거",
        "workload",
        "medium",
        ("CIS-K8S 5.2.9",),
        "capabilities.drop: [ALL]을 지정하세요.",
        _workload_rule("drops_all_capabilities", "drop ALL"),
    ),
    Control(
        "WL-04",
        "privileged 컨테이너 금지",
        "workload",
        "critical",
        ("CIS-K8S 5.2.2",),
        "privileged=false를 강제하세요.",
        _no_privileged,
    ),
    Control(
        "WL-05",
        "이미지 digest 고정",
        "workload",
        "medium",
        ("NSA-K8S",),
        "태그 대신 @sha256 digest로 이미지를 고정하세요.",
        _digest_pinned,
    ),
    Control(
        "WL-06",
        "Pod Security Admission 적용",
        "workload",
        "high",
        ("CIS-K8S 5.2.1",),
        "네임스페이스에 pod-security.kubernetes.io/enforce=restricted 라벨을 붙이세요.",
        _pod_security_admission,
    ),
    Control(
        "ID-01",
        "와일드카드 RBAC 금지",
        "identity",
        "high",
        ("CIS-K8S 5.1.3",),
        "verbs·resources에 *를 쓰는 Role을 필요한 권한으로 줄이세요.",
        _rbac_wildcards,
    ),
    Control(
        "ID-02",
        "Argo CD admin·익명 접근 차단",
        "identity",
        "high",
        ("CIS-K8S 5.1.1",),
        "admin.enabled=false, users.anonymous.enabled=false로 두세요.",
        _argocd_hardening,
    ),
    Control(
        "ID-03",
        "워크로드 ID 연동 (정적 키 미사용)",
        "identity",
        "high",
        ("AWS-FSBP IAM.6",),
        "EKS Pod Identity·GKE Workload Identity 또는 OIDC 워크로드 ID로 정적 자격증명을 대체하세요.",
        _declared(
            "workload_identity", "워크로드 ID로 클라우드 권한 위임", "정적 토큰·키로 권한 부여"
        ),
    ),
    Control(
        "DATA-01",
        "DB 전송 구간 TLS 강제",
        "data",
        "high",
        ("ONEPORT", "CIS-GCP 6.4"),
        "rds.force_ssl=1, Cloud SQL ssl_mode=ENCRYPTED_ONLY 또는 pg_hba hostssl만 허용하세요.",
        _declared("db_tls_required", "DB가 TLS 연결만 허용", "DB가 평문 연결 허용"),
    ),
    Control(
        "DATA-02",
        "DB 저장 데이터 암호화",
        "data",
        "high",
        ("AWS-FSBP RDS.3",),
        "스토리지 암호화를 켜세요.",
        _declared("db_encrypted_at_rest", "DB 스토리지 암호화", "DB 스토리지 미암호화"),
    ),
    Control(
        "DATA-03",
        "DB 백업",
        "data",
        "medium",
        ("AWS-FSBP RDS.11", "CIS-GCP 6.7"),
        "자동 백업 또는 주기 덤프를 구성하세요.",
        _declared("db_backups", "DB 백업 구성됨", "DB 백업 없음"),
    ),
    Control(
        "DATA-04",
        "외부 비밀 저장소 동기화",
        "data",
        "medium",
        ("NSA-K8S",),
        "External Secrets Operator로 Secrets Manager·Secret Manager·Vault에서 동기화하세요.",
        _external_secrets,
    ),
    Control(
        "DET-01",
        "위협 탐지",
        "detection",
        "high",
        ("AWS-FSBP GuardDuty.1",),
        "AWS는 GuardDuty, GCP는 Security Command Center 위협 탐지, 온프렘은 Falco 같은 런타임 탐지를 켜세요.",
        _declared("threat_detection", "위협 탐지 활성", "위협 탐지 없음"),
    ),
    Control(
        "DET-02",
        "보안 상태 집계 서비스",
        "detection",
        "medium",
        ("AWS-FSBP SecurityHub.1",),
        "Security Hub, Security Command Center 또는 동등한 posture 집계를 켜세요.",
        _declared("posture_service", "posture 집계 활성", "posture 집계 없음"),
    ),
    Control(
        "DET-03",
        "클러스터 감사 로그",
        "detection",
        "high",
        ("CIS-K8S 3.2.1", "AWS-FSBP EKS.8", "CIS-GCP 2.1"),
        "API server audit 로그를 켜고 보관하세요.",
        _declared("audit_log", "감사 로그 수집", "감사 로그 없음"),
    ),
    Control(
        "DET-04",
        "네트워크 흐름 로그",
        "detection",
        "low",
        ("AWS-FSBP EC2.6", "CIS-GCP 3.8"),
        "VPC Flow Logs(AWS·GCP) 또는 CNI 흐름 로그를 켜세요.",
        _declared("flow_logs", "흐름 로그 수집", "흐름 로그 없음"),
    ),
    Control(
        "GOV-01",
        "마이그레이션 정책 게이트 (OPA)",
        "governance",
        "high",
        ("ONEPORT SEC-02", "ONEPORT OPS-01"),
        "단계별 OPA 판정이 allow일 때만 진행하세요.",
        _declared("policy_gates", "OPA 게이트가 단계 진행을 판정", "정책 게이트 없음"),
    ),
    Control(
        "GOV-02",
        "네임스페이스 자원 상한",
        "governance",
        "low",
        ("NSA-K8S",),
        "ResourceQuota·LimitRange로 자원 상한을 두세요.",
        _resource_quota,
    ),
)


CONTROL_WHY = {
    "NET-01": "기본 차단 정책이 없으면 같은 클러스터의 다른 파드가 앱과 DB에 자유롭게 접근할 수 있어요. 한 파드가 뚫리면 옆으로 번지기 쉬워요.",
    "NET-02": "DB가 인터넷에 노출되면 비밀번호 대입이나 취약점 공격의 직접 대상이 돼요. 이전 중에는 원본과 대상 DB 모두 사설망에만 있어야 해요.",
    "NET-03": "외부 진입점이 평문 HTTP면 로그인 쿠키와 요청 내용이 네트워크에서 그대로 보여요.",
    "NET-04": "웹 방화벽이 없으면 SQL 인젝션, 봇 트래픽 같은 흔한 웹 공격을 앞단에서 걸러내지 못해요.",
    "WL-01": "컨테이너가 root로 실행되면 컨테이너 탈출 취약점이 곧 노드 권한 탈취로 이어질 수 있어요.",
    "WL-02": "루트 파일시스템에 쓸 수 있으면 공격자가 실행 파일을 바꾸거나 도구를 심을 수 있어요.",
    "WL-03": "남아 있는 Linux capability는 권한 상승에 쓰일 수 있어요. 앱에 필요한 것만 남겨야 해요.",
    "WL-04": "privileged 컨테이너는 노드의 모든 장치와 커널 기능에 접근할 수 있어 사실상 노드 root와 같아요.",
    "WL-05": "태그로만 지정한 이미지는 같은 이름으로 다른 내용이 올라오면 그대로 배포돼요. digest로 고정해야 검증한 이미지만 실행돼요.",
    "WL-06": "Pod Security Admission이 없으면 위의 컨테이너 규칙을 지키지 않은 파드도 그대로 생성돼요.",
    "ID-01": "* 권한을 가진 계정이 유출되면 네임스페이스 안의 모든 리소스(Secret 포함)를 읽고 바꿀 수 있어요.",
    "ID-02": "Argo CD의 admin이나 익명 접근이 열려 있으면 배포 설정을 바꿔 클러스터 전체에 원하는 것을 배포할 수 있어요.",
    "ID-03": "정적 토큰과 키는 유출돼도 알아채기 어렵고 교체도 수동이에요. 워크로드 ID는 짧게 쓰고 버리는 자격증명을 써요.",
    "DATA-01": "DB 연결이 평문이면 이전 중 오가는 데이터와 계정 정보가 네트워크에서 보여요.",
    "DATA-02": "디스크가 암호화되지 않으면 스냅샷이나 디스크가 유출될 때 데이터가 그대로 읽혀요.",
    "DATA-03": "백업이 없으면 이전 중 실수나 장애로 데이터가 깨졌을 때 되돌릴 지점이 없어요.",
    "DATA-04": "비밀값을 스크립트로 직접 만들면 교체 이력과 접근 기록이 남지 않아요.",
    "DET-01": "위협 탐지가 없으면 비정상 API 호출, 크립토마이닝, 유출 시도 같은 공격이 진행돼도 알 수 없어요.",
    "DET-02": "보안 상태를 모아 보는 곳이 없으면 보안 설정이 언제 바뀌었는지 계속 추적할 수 없어요.",
    "DET-03": "감사 로그가 없으면 누가 언제 무엇을 바꿨는지 사고 뒤에 확인할 수 없어요.",
    "DET-04": "흐름 로그가 없으면 차단된 접근 시도나 예상하지 못한 통신을 사후에 확인할 수 없어요.",
    "GOV-01": "정책 게이트 없이 전환하면 DB 노출이나 미동기화 같은 조건을 사람이 놓쳐도 그대로 진행돼요.",
    "GOV-02": "자원 상한이 없으면 한 앱의 폭주가 같은 클러스터의 다른 앱까지 멈추게 할 수 있어요.",
}
CONTROL_TARGETS = {
    "NET-02": ("postgres",),
    "DATA-01": ("postgres",),
    "DATA-02": ("postgres",),
    "DATA-03": ("postgres",),
    "NET-03": ("entry",),
    "NET-04": ("entry",),
    "WL-01": ("app",),
    "WL-02": ("app",),
    "WL-03": ("app",),
    "WL-04": ("app",),
    "WL-05": ("app",),
    "WL-06": ("app",),
    "NET-01": ("app",),
    "GOV-02": ("app",),
}


def affected_components(control_id: str, components: list[ComponentView]) -> list[str]:
    targets = CONTROL_TARGETS.get(control_id, ())
    return [c.name for c in components if c.kind in targets or c.layer in targets]


def evaluate(snapshot: EnvironmentSnapshot) -> dict[str, ControlResult]:
    return {control.id: control.check(snapshot) for control in CONTROLS}
