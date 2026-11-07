"""crosscloud 의 공통 자원 모델 — 원본 클라우드에서 파악한 구성(Inventory).

두 클라우드(AWS·GCP)를 한 형식으로 담는 값 객체다. 모든 값 객체는 바꿀 수
없고(frozen), 컬렉션은 tuple, 키-값은 tuple[KeyValue, ...] 로 둔다. 문자열
선택지는 Literal 로 닫는다.

JSON 형식은 schema/inventory.schema.json 이 정본이고, 둘 사이 변환은
crosscloud.inventory_json 이 맡는다.
"""

import dataclasses
import datetime
from typing import Literal

Cloud = Literal["aws", "gcp"]
ComputePlatform = Literal[
    "ec2", "ecs", "lambda", "eks", "gce", "cloud_run", "gke"
]
ResourceKind = Literal[
    "postgres_instance",
    "secret",
    "workload",
    "workload_identity",
    "load_balancer",
    "dns_record",
    "object_bucket",
    "kms_key",
    "kafka_cluster",
    "queue",
    "event_source",
    "schedule",
    "network_link",
    "container_registry",
]
SourceOfTruth = Literal["api", "iac_export", "kube_api"]
Certainty = Literal["mandatory", "potential"]
EntryKind = Literal["http", "queue_consumer", "event_trigger", "schedule"]
CouplingCategory = Literal[
    "db_auth_rds_iam",
    "db_auth_cloudsql_iam",
    "db_auth_password_url",
    "secret_sdk",
    "kafka_sasl_iam",
    "cloud_sdk_default_credentials",
]
Coverage = Literal["complete", "unknown"]
RestrictionKind = Literal[
    "scp",
    "rcp",
    "permissions_boundary",
    "resource_policy_deny",
    "session_policy",
    "gcp_deny_policy",
    "gcp_pab",
]


@dataclasses.dataclass(frozen=True)
class KeyValue:
    """속성 하나. 값은 JSON 스칼라만 받는다."""

    key: str
    value: str | int | float | bool | None


@dataclasses.dataclass(frozen=True)
class SecretRef:
    """비밀값을 가리키는 참조. 값 자체는 담지 않는다.

    Attributes:
        cloud: 비밀이 있는 클라우드.
        native_id: 클라우드 고유 식별자(ARN 또는 리소스 이름).
        version: "AWSCURRENT" · "latest" · 버전 번호.
    """

    cloud: Cloud
    native_id: str
    version: str


@dataclasses.dataclass(frozen=True)
class Resource:
    """워크로드 밖의 클라우드 자원 하나(DB·Secret·레지스트리 등)."""

    kind: ResourceKind
    cloud: Cloud
    native_id: str
    region: str
    attributes: tuple[KeyValue, ...]
    source_of_truth: SourceOfTruth


@dataclasses.dataclass(frozen=True)
class EnvEntry:
    """워크로드 env 하나와 그 분류.

    Attributes:
        key: env 이름.
        classification: classify_env 가 매긴 분류.
        value: plain_nonsecret 일 때만 값을 보존한다. 그 밖은 None 이다.
        confirmed_by: 보존 값을 사람이 확인(H1)했으면 확인자. 확인 전에는
            None 이고, 그 값은 대상 구성에 렌더링하지 않는다.
    """

    key: str
    classification: Literal[
        "secret_ref", "plain_nonsecret", "suspected_secret", "unknown", "empty"
    ]
    value: str | None
    confirmed_by: str | None


@dataclasses.dataclass(frozen=True)
class Entry:
    """워크로드로 요청이 들어오는 진입점 하나(HTTP·큐·이벤트·일정)."""

    kind: EntryKind
    source_id: str
    attributes: tuple[KeyValue, ...]


@dataclasses.dataclass(frozen=True)
class ImageRef:
    """digest 로 고정한 컨테이너 이미지."""

    registry_repo: str
    digest: str


@dataclasses.dataclass(frozen=True)
class Workload:
    """이전 대상 앱 하나.

    Attributes:
        name: Inventory 안에서 워크로드를 가리키는 이름.
        cloud: 워크로드가 도는 클라우드.
        platform: 컴퓨트 플랫폼.
        native_id: 플랫폼 고유 식별자.
        identity_id: 워크로드 신원(작업 역할·서비스 계정 등).
        image: 컨테이너 이미지. 컨테이너가 아니면 None.
        code_repo: 결합 탐지에 쓸 소스 저장소. 모르면 None.
        entries: 진입점.
        env: env 와 그 분류.
        secret_refs: 워크로드가 참조하는 비밀.
        db_user: 원본 앱 DB 사용자. 대상에서 같은 이름을 재사용한다.
        depends_on: 의존하는 자원·워크로드 식별자.
    """

    name: str
    cloud: Cloud
    platform: ComputePlatform
    native_id: str
    identity_id: str
    image: ImageRef | None
    code_repo: str | None
    entries: tuple[Entry, ...]
    env: tuple[EnvEntry, ...]
    secret_refs: tuple[SecretRef, ...]
    db_user: str | None
    depends_on: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class IdentityGrant:
    """신원에 붙은 허용·거부 한 건(정책 문장 하나에 대응)."""

    grant_id: str
    principal_id: str
    resource_id: str
    action: str
    effect: Literal["allow", "deny"]
    condition_raw: str | None
    origin: Literal[
        "identity_policy", "resource_policy", "trust_policy", "gcp_allow_policy"
    ]


@dataclasses.dataclass(frozen=True)
class Restriction:
    """신원 권한 위에 덮이는 제한 정책 한 건(SCP·권한 경계·GCP 거부 정책 등).

    Attributes:
        restriction_id: 정책 식별자.
        kind: 제한 정책 종류.
        scope: 정책이 걸린 범위(OU·계정·프로젝트 등).
        mode: deny 는 명시적 거부, ceiling 은 허용 상한.
        principals: 대상 주체.
        actions: 대상 동작.
        resources: 대상 자원.
        condition_raw: 조건 원문. 없으면 None.
        condition_parsed: 조건을 해석해 판정에 반영했는지.
    """

    restriction_id: str
    kind: RestrictionKind
    scope: str
    mode: Literal["deny", "ceiling"]
    principals: tuple[str, ...]
    actions: tuple[str, ...]
    resources: tuple[str, ...]
    condition_raw: str | None
    condition_parsed: bool


@dataclasses.dataclass(frozen=True)
class RestrictionScan:
    """제한 정책을 범위·종류별로 다 읽었는지(coverage)와 그 사유."""

    scope: str
    kind: RestrictionKind
    coverage: Coverage
    reason: str


@dataclasses.dataclass(frozen=True)
class InfraCoupling:
    """앱 코드가 인프라를 직접 다루는 자리 하나(결합 탐지 결과)."""

    workload: str
    location: str
    rule_id: str
    category: CouplingCategory
    certainty: Certainty


@dataclasses.dataclass(frozen=True)
class Omission:
    """수집에서 빠졌다고 알려진 대상 하나와 그 발견 경로."""

    subject: str
    detected_by: Literal[
        "export_vs_api", "permission_denied", "unsupported_type"
    ]
    acknowledged_by: str | None


@dataclasses.dataclass(frozen=True)
class CollectionAttestation:
    """수집이 비밀값을 읽지도 남기지도 않았다는 증거.

    Attributes:
        secret_value_calls: 수집 동안의 비밀값 조회 호출 수(AWS CloudTrail).
            GCP 처럼 감사 로그로 셀 수 없으면 None.
        collector_can_read_secret_values: 수집 신원이 비밀값을 읽을 권한을
            가졌는지.
        cai_viewer_bound: 수집 신원에 Cloud Asset Inventory 뷰어 역할이
            붙었는지.
        suspected_values_retained: 비밀로 의심되거나 분류가 불명인데 값을
            남긴 env 수. 스키마가 0 만 받는다.
        evidence_ref: 증거(조회 결과)의 위치.
    """

    secret_value_calls: int | None
    collector_can_read_secret_values: bool
    cai_viewer_bound: bool
    suspected_values_retained: int
    evidence_ref: str


@dataclasses.dataclass(frozen=True)
class Inventory:
    """원본 클라우드 한 곳에서 파악한 구성 전체."""

    cloud: Cloud
    collected_at: datetime.datetime
    resources: tuple[Resource, ...]
    workloads: tuple[Workload, ...]
    grants: tuple[IdentityGrant, ...]
    restrictions: tuple[Restriction, ...]
    restriction_scans: tuple[RestrictionScan, ...]
    couplings: tuple[InfraCoupling, ...]
    omissions: tuple[Omission, ...]
    attestation: CollectionAttestation
