"""이관 결과 파일 형식 — bundle.json(AWS→GCP)과 manifest.json(GCP→AWS).

bundle.json 은 오프라인 검증에 필요한 것을 모두 담는다(평가 20 D2 축소).

*   objects: 경로 대응표(원래 경로 → 새 경로)와 객체별 원본 바이트 SHA-256.
*   digest_signatures: 다이제스트마다 원위치 S3 메타데이터의 서명·알고리즘.
    체인 끝 다이제스트의 서명은 오프라인에서 달리 얻을 수 없어(RC-A21)
    반드시 여기 있어야 하고, 중간 다이제스트 것도 함께 둬서 하나가
    지워져도 나머지를 각각 검증할 수 있게 한다.
*   public_keys: ListPublicKeys 결과(지문·DER·유효 기간).

manifest.json 은 GCP 쪽 원본에 공급자 서명이 없음을 그대로 적는다
(evidence: none, 평가 20 D3·D4). 해시는 이관 중 불변만 증명한다.

두 파일 모두 대상 버킷에서 다시 읽을 때는 외부 입력으로 보고 형식을
검사한다(InvalidArchiveFileError).
"""

import base64
import binascii
import dataclasses
import datetime
from collections.abc import Mapping, Sequence
from typing import Any

from crosscloud.audit_copy import cloudtrail_verify, copier

BUNDLE_FORMAT = "crosscloud.audit_copy.bundle/v1"
MANIFEST_FORMAT = "crosscloud.audit_copy.manifest/v1"
GCP_LIMITATION = "공급자 서명 없음 — 이관 중 불변만 증명"
_SIGNATURE = "signature"
_SIGNATURE_ALGORITHM = "signature-algorithm"
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclasses.dataclass(frozen=True)
class PublicKey:
    """CloudTrail 공개키 하나(ListPublicKeys 결과 한 항목).

    Attributes:
        fingerprint: 공개키 지문(다이제스트의 digestPublicKeyFingerprint).
        value: PKCS#1 DER 바이트.
        validity_start: 유효 시작 시각(UTC, ISO 8601).
        validity_end: 유효 끝 시각(UTC, ISO 8601).
    """

    fingerprint: str
    value: bytes
    validity_start: str
    validity_end: str


@dataclasses.dataclass(frozen=True)
class Bundle:
    """bundle.json 을 읽은 결과 — 오프라인 검증기의 입력."""

    objects: tuple[copier.CopiedObject, ...]
    signatures: Mapping[str, cloudtrail_verify.DigestSignature]
    public_keys: Mapping[str, bytes]


class InvalidArchiveFileError(ValueError):
    """bundle.json·manifest.json 이 이 모듈의 형식이 아니다."""


def public_keys_from_list_response(
    response: Mapping[str, Any],
) -> tuple[PublicKey, ...]:
    """boto3 cloudtrail.list_public_keys 응답을 PublicKey 로 바꾼다.

    Args:
        response: list_public_keys 응답(PublicKeyList 의 Value 는 bytes).

    Returns:
        응답 순서 그대로의 공개키들.
    """
    return tuple(
        PublicKey(
            fingerprint=entry["Fingerprint"],
            value=entry["Value"],
            validity_start=_format_time(entry["ValidityStartTime"]),
            validity_end=_format_time(entry["ValidityEndTime"]),
        )
        for entry in response["PublicKeyList"]
    )


def bundle_to_json(
    run_id: str,
    copied: Sequence[copier.CopiedObject],
    public_keys: Sequence[PublicKey],
) -> dict[str, Any]:
    """AWS→GCP 이관의 bundle.json 문서를 만든다."""
    signatures = []
    for item in copied:
        metadata = dict(item.metadata)
        if _SIGNATURE in metadata:
            signatures.append(
                {
                    "source": item.source_uri,
                    "signature": metadata[_SIGNATURE],
                    "algorithm": metadata.get(_SIGNATURE_ALGORITHM, ""),
                }
            )
    return {
        "format": BUNDLE_FORMAT,
        "direction": "aws_to_gcp",
        "run_id": run_id,
        "objects": [_object_to_json(item) for item in copied],
        "digest_signatures": signatures,
        "public_keys": [
            {
                "fingerprint": key.fingerprint,
                "value": base64.b64encode(key.value).decode("ascii"),
                "validity_start": key.validity_start,
                "validity_end": key.validity_end,
            }
            for key in public_keys
        ],
    }


def bundle_from_json(document: object) -> Bundle:
    """bundle.json 문서를 검사해 Bundle 로 읽는다.

    Raises:
        InvalidArchiveFileError: 형식·필드가 맞지 않을 때.
    """
    body = _expect_format(document, BUNDLE_FORMAT)
    try:
        return Bundle(
            objects=_objects_from_json(body["objects"]),
            signatures={
                entry["source"]: cloudtrail_verify.DigestSignature(
                    signature=entry["signature"], algorithm=entry["algorithm"]
                )
                for entry in body["digest_signatures"]
            },
            public_keys={
                entry["fingerprint"]: base64.b64decode(
                    entry["value"], validate=True
                )
                for entry in body["public_keys"]
            },
        )
    except (KeyError, TypeError, binascii.Error) as e:
        raise InvalidArchiveFileError(f"bundle 필드 오류: {e!r}") from e


def manifest_to_json(
    run_id: str, copied: Sequence[copier.CopiedObject]
) -> dict[str, Any]:
    """GCP→AWS 이관의 manifest.json 문서를 만든다."""
    return {
        "format": MANIFEST_FORMAT,
        "direction": "gcp_to_aws",
        "run_id": run_id,
        "evidence": "none",
        "limitation": GCP_LIMITATION,
        "objects": [_object_to_json(item) for item in copied],
    }


def manifest_from_json(document: object) -> tuple[copier.CopiedObject, ...]:
    """manifest.json 문서를 검사해 객체 목록으로 읽는다.

    Raises:
        InvalidArchiveFileError: 형식·필드가 맞지 않을 때.
    """
    body = _expect_format(document, MANIFEST_FORMAT)
    try:
        return _objects_from_json(body["objects"])
    except (KeyError, TypeError) as e:
        raise InvalidArchiveFileError(f"manifest 필드 오류: {e!r}") from e


def _object_to_json(item: copier.CopiedObject) -> dict[str, Any]:
    return {
        "source": item.source_uri,
        "target": item.target_uri,
        "sha256": item.sha256,
        "size": item.size,
    }


def _objects_from_json(entries: object) -> tuple[copier.CopiedObject, ...]:
    if not isinstance(entries, list):
        raise TypeError("objects 는 목록이어야 함")
    return tuple(
        copier.CopiedObject(
            source_uri=entry["source"],
            target_uri=entry["target"],
            sha256=entry["sha256"],
            size=entry["size"],
            metadata=(),
        )
        for entry in entries
    )


def _expect_format(document: object, expected: str) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("format") != expected:
        raise InvalidArchiveFileError(f"{expected} 형식이 아님")
    return document


def _format_time(value: datetime.datetime) -> str:
    return value.astimezone(datetime.UTC).strftime(_TIME_FORMAT)
