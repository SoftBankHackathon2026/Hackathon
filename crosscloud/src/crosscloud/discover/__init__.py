"""원본 클라우드의 구성을 읽는다(파악 단계).

클라우드마다 읽는 쪽이 하나씩 있다(aws.AwsInventoryReader·
gcp.GcpInventoryReader). 둘 다 읽기 전용이고 비밀값을 조회하지 않는다
(프로필 4절 — 수집기는 Secret 을 읽지 않는다).
"""

from typing import Protocol

from crosscloud import model


class InventoryReader(Protocol):
    """원본 클라우드 한 곳의 구성을 Inventory 로 읽는다."""

    def read(self) -> model.Inventory:
        """구성을 읽는다."""
        ...


def identity_omission(identity_id: str) -> model.Omission:
    """워크로드 신원의 권한을 수집하지 않았다는 Omission 을 만든다.

    작업 역할·서비스 계정으로 닿는 대응표 밖 의존(S3·SQS·ElastiCache 등)은
    읽지 않는다. 그 빈자리를 사람 결정으로 올리기 위한 표시다.

    Args:
        identity_id: 작업 역할 ARN 또는 서비스 계정.

    Returns:
        `identity_grants:<신원>` Omission.
    """
    return model.Omission(
        f"identity_grants:{identity_id}", "unsupported_type", None
    )


def unattested(
    workloads: tuple[model.Workload, ...],
) -> model.CollectionAttestation:
    """수집 증거를 모으지 않았다는 CollectionAttestation 을 만든다.

    본선은 수집 증거(CloudTrail 조회·권한 판정)를 만들지 않는다(사용자 결정
    2026-11-07 시험 환경 단순화). 그래서 확인하지 않은 값은 보수적으로 둔다
    — 수집 신원이 비밀값을 읽을 수 있다고 본다.

    Args:
        workloads: 이번에 읽은 워크로드. 남긴 비밀 의심 값을 센다.

    Returns:
        증거 없음을 나타내는 CollectionAttestation.
    """
    retained = sum(
        1
        for workload in workloads
        for entry in workload.env
        if entry.classification != "plain_nonsecret" and entry.value is not None
    )
    return model.CollectionAttestation(
        secret_value_calls=None,
        collector_can_read_secret_values=True,
        cai_viewer_bound=False,
        suspected_values_retained=retained,
        evidence_ref="not_collected",
    )
