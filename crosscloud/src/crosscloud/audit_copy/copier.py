"""원본 바이트 그대로의 객체 복사기 — 객체별 SHA-256 과 경로 대응표를 남긴다.

전송 서비스(STS·DataSync) 대신 SDK 로 직접 받고 올린다. 그래서 gzip
객체의 압축 해제 변환이나 메타데이터 보존 여부에 기대지 않는다(평가 20
D2, RC-A13). 쓴 뒤에는 대상에서 다시 읽어 해시가 같은지 확인한다.

  copied = copier.copy_objects(s3, "AWSLogs/", gcs, "ccx-r1/objects/")
"""

import dataclasses
import hashlib

from crosscloud.audit_copy import object_store


@dataclasses.dataclass(frozen=True)
class CopiedObject:
    """복사한 객체 하나 — 경로 대응표 한 줄과 원본 바이트 SHA-256.

    Attributes:
        source_uri: 원래 경로(s3://… 또는 gs://…).
        target_uri: 새 경로.
        sha256: 원본 바이트(압축 해제 전)의 SHA-256 16진 문자열.
        size: 바이트 수.
        metadata: 원본 객체의 사용자 메타데이터(예: 다이제스트 서명).
    """

    source_uri: str
    target_uri: str
    sha256: str
    size: int
    metadata: tuple[tuple[str, str], ...]


class CopyIntegrityError(Exception):
    """복사 중 원본이 사라졌거나, 대상에 쓴 바이트가 원본과 다르다."""


def copy_objects(
    source: object_store.ObjectStore,
    source_prefix: str,
    target: object_store.ObjectStore,
    target_prefix: str,
) -> tuple[CopiedObject, ...]:
    """source_prefix 아래 모든 객체를 target_prefix + 원래 키로 복사한다.

    Args:
        source: 원본 버킷.
        source_prefix: 복사할 키 접두사.
        target: 대상 버킷.
        target_prefix: 대상 키 앞에 붙일 접두사.

    Returns:
        복사한 객체들(원본 키 순).

    Raises:
        CopyIntegrityError: 원본이 목록과 읽기 사이에 사라졌거나, 대상에서
            다시 읽은 바이트의 SHA-256 이 원본과 다를 때.
        object_store.ObjectExistsError: 대상 키에 이미 객체가 있을 때.
    """
    copied = []
    for key in source.list_keys(source_prefix):
        stored = source.get(key)
        if stored is None:
            raise CopyIntegrityError(f"원본이 사라짐: {source.uri(key)}")
        target_key = target_prefix + key
        target.put(target_key, stored)
        sha256 = hashlib.sha256(stored.data).hexdigest()
        written = target.get(target_key)
        if (
            written is None
            or hashlib.sha256(written.data).hexdigest() != sha256
        ):
            raise CopyIntegrityError(
                f"대상 바이트가 원본과 다름: {target.uri(target_key)}"
            )
        copied.append(
            CopiedObject(
                source_uri=source.uri(key),
                target_uri=target.uri(target_key),
                sha256=sha256,
                size=len(stored.data),
                metadata=stored.metadata,
            )
        )
    return tuple(copied)
