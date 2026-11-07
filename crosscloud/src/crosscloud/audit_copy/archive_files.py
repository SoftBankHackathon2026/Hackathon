"""이관 결과 파일 형식 — bundle.json·manifest.json 과 로컬 이관 기록.

bundle.json(AWS→GCP, 대상 버킷)은 오프라인 검증에 쓸 자료를 담는다(평가 20
D2 축소).

*   objects: 경로 대응표(원래 경로 → 새 경로)와 객체별 원본 바이트 SHA-256.
*   digest_signatures: 다이제스트마다 원위치 S3 메타데이터의 서명·알고리즘.
    체인 끝 다이제스트의 서명은 오프라인에서 달리 얻을 수 없어(RC-A21)
    반드시 여기 있어야 하고, 중간 다이제스트 것도 함께 둬서 하나가
    지워져도 나머지를 각각 검증할 수 있게 한다.
*   public_keys: 이관 때 받은 ListPublicKeys 결과. 검증의 신뢰 기준이 아니라
    운영자가 따로 보관한 공개키와 대조하는 용도다.

manifest.json(GCP→AWS, 대상 버킷)은 GCP 쪽 원본에 공급자 서명이 없음을
그대로 적는다(evidence: none, 평가 20 D3·D4). 해시는 이관 중 불변만 증명한다.

이관 기록(Receipt)은 대상 버킷 밖, 운영자 PC 에 두는 파일이다. bundle·
manifest 의 SHA-256·객체 수·체인 시작/끝 다이제스트를 적어 두고, 검증은 이
기록과 맞는 bundle·manifest 만 믿는다. 대상 버킷만 바꾼 위조는 여기서 걸린다.

대상 버킷에서 다시 읽는 파일과 운영자 입력 파일은 모두 외부 입력으로 보고
형식을 검사한다(InvalidArchiveFileError).
"""

import base64
import binascii
import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from crosscloud.audit_copy import cloudtrail_verify, copier

BUNDLE_FORMAT = "crosscloud.audit_copy.bundle/v1"
MANIFEST_FORMAT = "crosscloud.audit_copy.manifest/v1"
RECEIPT_FORMAT = "crosscloud.audit_copy.receipt/v1"
GCP_LIMITATION = "공급자 서명 없음 — 이관 중 불변만 증명"
DIRECTIONS = ("aws_to_gcp", "gcp_to_aws")
Direction = Literal["aws_to_gcp", "gcp_to_aws"]
_SIGNATURE = "signature"
_SIGNATURE_ALGORITHM = "signature-algorithm"


@dataclasses.dataclass(frozen=True)
class PublicKey:
    """CloudTrail 공개키 하나(ListPublicKeys 결과 한 항목).

    Attributes:
        fingerprint: 공개키 지문(다이제스트의 digestPublicKeyFingerprint).
        value: PKCS#1 DER 바이트.
        validity_start: 유효 시작 시각(출력 문자열 그대로).
        validity_end: 유효 끝 시각(출력 문자열 그대로).
    """

    fingerprint: str
    value: bytes
    validity_start: str
    validity_end: str


@dataclasses.dataclass(frozen=True)
class Bundle:
    """bundle.json 을 읽은 결과."""

    run_id: str
    objects: tuple[copier.CopiedObject, ...]
    signatures: Mapping[str, cloudtrail_verify.DigestSignature]
    public_keys: Mapping[str, bytes]


@dataclasses.dataclass(frozen=True)
class Receipt:
    """대상 버킷 밖에 두는 이관 기록 — 검증의 기준값.

    Attributes:
        direction: 이관 방향.
        run_id: 이관 실행 식별자(ccx-<run> 의 run).
        archive_file: 대상 버킷의 bundle.json·manifest.json 경로.
        archive_file_sha256: 그 파일을 쓴 바이트의 SHA-256.
        object_count: 옮긴 객체 수.
        chains: 체인 이름 → 체인 시작·끝(표준) 다이제스트. GCP→AWS 는 빈 값.
    """

    direction: Direction
    run_id: str
    archive_file: str
    archive_file_sha256: str
    object_count: int
    chains: Mapping[str, cloudtrail_verify.ChainBounds]


class InvalidArchiveFileError(ValueError):
    """bundle·manifest·이관 기록·공개키 파일이 기대한 형식이 아니다."""


def public_keys_from_cli_output(document: object) -> tuple[PublicKey, ...]:
    """`aws cloudtrail list-public-keys` 출력(JSON)을 PublicKey 로 읽는다.

    온라인 단계에서 받아 운영자가 보관한 파일이 입력이다(RC-A21). Value 는
    base64 로 인코딩된 PKCS#1 DER 이고, 시각은 출력 문자열 그대로 둔다.

    Args:
        document: 출력 JSON 을 json.loads 로 읽은 값.

    Returns:
        출력 순서 그대로의 공개키들.

    Raises:
        InvalidArchiveFileError: PublicKeyList 형식이 아닐 때.
    """
    try:
        return tuple(
            PublicKey(
                fingerprint=_text(entry, "Fingerprint"),
                value=base64.b64decode(_text(entry, "Value"), validate=True),
                validity_start=_text(entry, "ValidityStartTime"),
                validity_end=_text(entry, "ValidityEndTime"),
            )
            for entry in _list(document, "PublicKeyList")
        )
    except (KeyError, TypeError, binascii.Error) as e:
        raise InvalidArchiveFileError(f"PublicKeyList 형식 오류: {e!r}") from e


def merge_public_keys(
    groups: Iterable[Sequence[PublicKey]],
) -> tuple[PublicKey, ...]:
    """리전별 공개키 파일 여러 개를 지문 기준으로 합친다.

    Raises:
        InvalidArchiveFileError: 같은 지문에 다른 키 값이 있을 때.
    """
    merged: dict[str, PublicKey] = {}
    for group in groups:
        for key in group:
            known = merged.setdefault(key.fingerprint, key)
            if known.value != key.value:
                raise InvalidArchiveFileError(
                    f"지문 {key.fingerprint} 의 공개키 값이 파일마다 다름"
                )
    return tuple(merged.values())


def public_key_value_to_json(value: bytes) -> str:
    """공개키 DER 바이트를 bundle.json 에 쓰는 base64 문자열로 바꾼다."""
    return base64.b64encode(value).decode("ascii")


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
                "value": public_key_value_to_json(key.value),
                "validity_start": key.validity_start,
                "validity_end": key.validity_end,
            }
            for key in public_keys
        ],
    }


def bundle_from_json(document: object) -> Bundle:
    """bundle.json 문서를 검사해 Bundle 로 읽는다.

    Raises:
        InvalidArchiveFileError: 형식·필드가 맞지 않거나 원래 경로·새 경로가
            중복될 때.
    """
    body = _expect_format(document, BUNDLE_FORMAT)
    try:
        signatures = {}
        for entry in _list(body, "digest_signatures"):
            source = _text(entry, "source")
            if source in signatures:
                raise InvalidArchiveFileError(f"서명 중복: {source}")
            signatures[source] = cloudtrail_verify.DigestSignature(
                signature=_text(entry, "signature"),
                algorithm=_text(entry, "algorithm"),
            )
        return Bundle(
            run_id=_text(body, "run_id"),
            objects=_objects_from_json(_list(body, "objects")),
            signatures=signatures,
            public_keys={
                _text(entry, "fingerprint"): base64.b64decode(
                    _text(entry, "value"), validate=True
                )
                for entry in _list(body, "public_keys")
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
        InvalidArchiveFileError: 형식·필드가 맞지 않거나 경로가 중복될 때.
    """
    body = _expect_format(document, MANIFEST_FORMAT)
    try:
        return _objects_from_json(_list(body, "objects"))
    except (KeyError, TypeError) as e:
        raise InvalidArchiveFileError(f"manifest 필드 오류: {e!r}") from e


def receipt_to_json(receipt: Receipt) -> dict[str, Any]:
    """이관 기록을 운영자 PC 에 쓸 JSON 문서로 바꾼다."""
    return {
        "format": RECEIPT_FORMAT,
        "direction": receipt.direction,
        "run_id": receipt.run_id,
        "archive_file": receipt.archive_file,
        "archive_file_sha256": receipt.archive_file_sha256,
        "object_count": receipt.object_count,
        "chains": [
            {
                "chain": chain,
                "start_digest": bounds.oldest,
                "end_digest": bounds.newest,
            }
            for chain, bounds in sorted(receipt.chains.items())
        ],
    }


def receipt_from_json(document: object) -> Receipt:
    """이관 기록 파일을 검사해 Receipt 로 읽는다.

    Raises:
        InvalidArchiveFileError: 형식·필드가 맞지 않을 때.
    """
    body = _expect_format(document, RECEIPT_FORMAT)
    try:
        direction = _text(body, "direction")
        if direction not in DIRECTIONS:
            raise InvalidArchiveFileError(f"모르는 방향: {direction}")
        return Receipt(
            direction=direction,
            run_id=_text(body, "run_id"),
            archive_file=_text(body, "archive_file"),
            archive_file_sha256=_text(body, "archive_file_sha256"),
            object_count=_integer(body, "object_count"),
            chains={
                _text(entry, "chain"): cloudtrail_verify.ChainBounds(
                    oldest=_text(entry, "start_digest"),
                    newest=_text(entry, "end_digest"),
                )
                for entry in _list(body, "chains")
            },
        )
    except (KeyError, TypeError) as e:
        raise InvalidArchiveFileError(f"이관 기록 필드 오류: {e!r}") from e


def _object_to_json(item: copier.CopiedObject) -> dict[str, Any]:
    return {
        "source": item.source_uri,
        "target": item.target_uri,
        "sha256": item.sha256,
        "size": item.size,
    }


def _objects_from_json(
    entries: list[Any],
) -> tuple[copier.CopiedObject, ...]:
    objects = tuple(
        copier.CopiedObject(
            source_uri=_text(entry, "source"),
            target_uri=_text(entry, "target"),
            sha256=_text(entry, "sha256"),
            size=_integer(entry, "size"),
            metadata=(),
        )
        for entry in entries
    )
    for field in ("source_uri", "target_uri"):
        values = [getattr(item, field) for item in objects]
        if len(set(values)) != len(values):
            raise InvalidArchiveFileError(f"objects 의 경로 중복({field})")
    return objects


def _expect_format(document: object, expected: str) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("format") != expected:
        raise InvalidArchiveFileError(f"{expected} 형식이 아님")
    return document


def _text(entry: object, field: str) -> str:
    value = _field(entry, field)
    if not isinstance(value, str):
        raise TypeError(f"{field} 는 문자열이어야 함")
    return value


def _integer(entry: object, field: str) -> int:
    value = _field(entry, field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field} 는 정수여야 함")
    return value


def _list(entry: object, field: str) -> list[Any]:
    value = _field(entry, field)
    if not isinstance(value, list):
        raise TypeError(f"{field} 는 목록이어야 함")
    return value


def _field(entry: object, field: str) -> object:
    if not isinstance(entry, dict):
        raise TypeError(f"{field} 를 담은 객체가 아님")
    return entry[field]
