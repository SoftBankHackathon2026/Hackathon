"""감사 기록 이관의 두 방향과, 이관한 결과의 검증.

대상 버킷 안 배치(run 하나 = 접두사 하나):

  ccx-<run>/objects/<원래 키>   원본 바이트 그대로
  ccx-<run>/bundle.json        AWS→GCP 검증 묶음
  ccx-<run>/manifest.json      GCP→AWS 해시 매니페스트

AWS→GCP 는 CloudTrail 로그·다이제스트를 옮기고 bundle.json 을 남긴다.
GCP→AWS 는 `gcloud logging copy` 가 GCS 에 쓴 결과 파일을 옮기고
manifest.json 을 남긴다. 두 방향 모두 이관 기록(Receipt)을 돌려주고,
호출자는 그것을 대상 버킷 밖(운영자 PC)에 보관한다.

검증은 대상 버킷만 읽되(오프라인), 신뢰 기준은 대상 버킷 밖에서 받는다.
이관 기록과 SHA-256 이 같은 bundle·manifest 만 믿고, 공개키는 운영자가
보관한 list-public-keys 파일 것만 쓴다.

결과(result)는 셋 중 하나다. invalid: 변조·누락·기록 불일치.
no_evidence: 어긋난 것은 없지만 서명으로 확인한 객체도 없음(GCP→AWS 는 늘
이것이다). valid: 서명으로 확인한 객체가 있고 어긋난 것이 없음.
"""

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from crosscloud.audit_copy import (
    archive_files,
    cloudtrail_verify,
    copier,
    object_store,
)

# Terraform name_prefix(ccx-<run>)와 같은 규약: run 은 영문 소문자·숫자 1–16자.
_RUN_ID = re.compile(r"[a-z0-9]{1,16}")
_JSON = "application/json"
_BUNDLE = "bundle.json"
_MANIFEST = "manifest.json"


class _UntrustedArchiveFileError(Exception):
    """bundle·manifest 를 믿을 수 없다. 메시지가 판정 사유가 된다."""


def archive_cloudtrail(
    source: object_store.ObjectStore,
    source_prefix: str,
    target: object_store.ObjectStore,
    run_id: str,
    public_keys: Sequence[archive_files.PublicKey],
) -> archive_files.Receipt:
    """AWS→GCP: trail 버킷의 로그·다이제스트를 옮기고 bundle.json 을 쓴다.

    Args:
        source: trail 버킷(원위치). 읽기만 한다.
        source_prefix: 옮길 키 접두사(예: "AWSLogs/<계정>/").
        target: 대상 보존 버킷.
        run_id: 이관 실행 식별자 run(ccx-<run> 의 run). 영문 소문자·숫자
            1–16자.
        public_keys: 해당 기간 ListPublicKeys 결과. bundle 에 대조용으로 둔다.

    Returns:
        이관 기록. 대상 버킷 밖에 보관해야 검증의 기준값이 된다.

    Raises:
        ValueError: run_id 형식이 틀렸을 때.
        copier.CopyIntegrityError: 복사한 바이트가 원본과 다를 때.
        object_store.ObjectExistsError: 같은 run 이 이미 있을 때.
    """
    prefix = _run_prefix(run_id)
    copied = copier.copy_objects(
        source, source_prefix, target, prefix + "objects/"
    )
    bundle = archive_files.bundle_to_json(run_id, copied, public_keys)
    sha256 = _put_json(target, prefix + _BUNDLE, bundle)
    return archive_files.Receipt(
        direction="aws_to_gcp",
        run_id=run_id,
        archive_file=target.uri(prefix + _BUNDLE),
        archive_file_sha256=sha256,
        object_count=len(copied),
        chains=cloudtrail_verify.chain_bounds(
            item.source_uri for item in copied
        ),
    )


def archive_gcp_export(
    source: object_store.ObjectStore,
    source_prefix: str,
    target: object_store.ObjectStore,
    run_id: str,
) -> archive_files.Receipt:
    """GCP→AWS: logging copy 결과 파일을 옮기고 manifest.json 을 쓴다.

    Args:
        source: `gcloud logging copy` 가 쓴 GCS 버킷. 읽기만 한다.
        source_prefix: 옮길 키 접두사.
        target: 대상 S3 Object Lock 버킷.
        run_id: 이관 실행 식별자.

    Returns:
        이관 기록. 대상 버킷 밖에 보관해야 검증의 기준값이 된다.

    Raises:
        ValueError: run_id 형식이 틀렸을 때.
        copier.CopyIntegrityError: 복사한 바이트가 원본과 다를 때.
        object_store.ObjectExistsError: 같은 run 이 이미 있을 때.
    """
    prefix = _run_prefix(run_id)
    copied = copier.copy_objects(
        source, source_prefix, target, prefix + "objects/"
    )
    manifest = archive_files.manifest_to_json(run_id, copied)
    sha256 = _put_json(target, prefix + _MANIFEST, manifest)
    return archive_files.Receipt(
        direction="gcp_to_aws",
        run_id=run_id,
        archive_file=target.uri(prefix + _MANIFEST),
        archive_file_sha256=sha256,
        object_count=len(copied),
        chains={},
    )


def verify_cloudtrail_archive(
    target: object_store.ObjectStore,
    run_id: str,
    receipt: archive_files.Receipt,
    public_keys: Sequence[archive_files.PublicKey],
) -> dict[str, Any]:
    """옮긴 CloudTrail 기록을 대상 버킷만 읽어 검증한다.

    순서: 이관 기록과 bundle.json 대조 → 공개키 대조 → 객체별 원본 바이트
    SHA-256(transfer) → 다이제스트 체인 서명·로그 해시(cloudtrail).

    Args:
        target: 대상 GCS 버킷.
        run_id: 검증할 run.
        receipt: 이관 때 받은 기록(대상 버킷 밖 보관본).
        public_keys: 운영자가 보관한 list-public-keys 결과. 서명 검증은
            이것만 쓴다.

    Returns:
        {"direction", "result", "archive_file", ...}. result 는 valid·
        invalid·no_evidence.
    """
    try:
        document = _load_trusted(target, run_id, receipt, "aws_to_gcp", _BUNDLE)
        bundle = archive_files.bundle_from_json(document)
        _check_rows(target, run_id, receipt, bundle.objects)
    except (
        _UntrustedArchiveFileError,
        archive_files.InvalidArchiveFileError,
    ) as e:
        return _untrusted("aws_to_gcp", receipt, str(e))
    trusted_keys = {key.fingerprint: key.value for key in public_keys}
    key_check = _compare_public_keys(bundle.public_keys, trusted_keys)
    read_target = _target_reader(target)
    transfer = _check_transfer(bundle.objects, read_target)
    by_source = {item.source_uri: item.target_uri for item in bundle.objects}
    report = cloudtrail_verify.verify_cloudtrail(
        object_uris=tuple(by_source),
        signatures=bundle.signatures,
        public_keys=trusted_keys,
        read=lambda uri: read_target(by_source.get(uri, "")),
        chain_bounds=receipt.chains,
    )
    failed = key_check["status"] != "valid" or not _all_valid(transfer)
    return {
        "direction": "aws_to_gcp",
        "result": "invalid" if failed else report.result,
        "archive_file": {"uri": receipt.archive_file, "status": "valid"},
        "public_keys": key_check,
        "transfer": transfer,
        "cloudtrail": cloudtrail_verify.report_to_dict(report),
    }


def verify_gcp_archive(
    target: object_store.ObjectStore,
    run_id: str,
    receipt: archive_files.Receipt,
) -> dict[str, Any]:
    """옮긴 GCP 감사 기록을 이관 기록·manifest.json 해시로 대조한다.

    공급자 서명이 없으므로 어긋난 것이 없어도 결과는 no_evidence 다.
    전송 대조 결과는 transfer_result 에 따로 둔다.

    Args:
        target: 대상 S3 버킷.
        run_id: 검증할 run.
        receipt: 이관 때 받은 기록(대상 버킷 밖 보관본).
    """
    try:
        document = _load_trusted(
            target, run_id, receipt, "gcp_to_aws", _MANIFEST
        )
        objects = archive_files.manifest_from_json(document)
        _check_rows(target, run_id, receipt, objects)
    except (
        _UntrustedArchiveFileError,
        archive_files.InvalidArchiveFileError,
    ) as e:
        return _untrusted("gcp_to_aws", receipt, str(e))
    transfer = _check_transfer(objects, _target_reader(target))
    transfer_ok = _all_valid(transfer)
    return {
        "direction": "gcp_to_aws",
        "result": "no_evidence" if transfer_ok else "invalid",
        "transfer_result": "valid" if transfer_ok else "invalid",
        "evidence": "none",
        "limitation": archive_files.GCP_LIMITATION,
        "archive_file": {"uri": receipt.archive_file, "status": "valid"},
        "transfer": transfer,
    }


def _load_trusted(
    target: object_store.ObjectStore,
    run_id: str,
    receipt: archive_files.Receipt,
    direction: str,
    name: str,
) -> object:
    """이관 기록과 맞는 bundle·manifest 만 읽어 돌려준다."""
    key = _run_prefix(run_id) + name
    if receipt.direction != direction or receipt.run_id != run_id:
        raise _UntrustedArchiveFileError(
            "이관 기록의 방향·run 이 검증 인자와 다름"
        )
    if receipt.archive_file != target.uri(key):
        raise _UntrustedArchiveFileError("이관 기록의 파일 경로가 다름")
    stored = target.get(key)
    if stored is None:
        raise _UntrustedArchiveFileError("없음")
    if hashlib.sha256(stored.data).hexdigest() != receipt.archive_file_sha256:
        raise _UntrustedArchiveFileError("이관 기록의 SHA-256 과 다름")
    try:
        document = json.loads(stored.data)
    except ValueError as e:
        raise _UntrustedArchiveFileError("JSON 이 아님") from e
    if not isinstance(document, dict) or document.get("run_id") != run_id:
        raise _UntrustedArchiveFileError("파일의 run_id 가 검증 인자와 다름")
    return document


def _check_rows(
    target: object_store.ObjectStore,
    run_id: str,
    receipt: archive_files.Receipt,
    objects: Sequence[copier.CopiedObject],
) -> None:
    """객체 수가 기록과 같고, 모든 새 경로가 이 run 의 접두사 안인지 본다."""
    if len(objects) != receipt.object_count:
        raise _UntrustedArchiveFileError("객체 수가 이관 기록과 다름")
    root = target.uri(_run_prefix(run_id) + "objects/")
    if any(not item.target_uri.startswith(root) for item in objects):
        raise _UntrustedArchiveFileError("새 경로가 이 run 의 접두사 밖")


def _compare_public_keys(
    in_bundle: Mapping[str, bytes], trusted: Mapping[str, bytes]
) -> dict[str, Any]:
    """Bundle 공개키는 대조만 한다. 같은 지문에 다른 값이면 invalid."""
    mismatched = sorted(
        fingerprint
        for fingerprint, value in in_bundle.items()
        if fingerprint in trusted and trusted[fingerprint] != value
    )
    if mismatched:
        return {
            "status": "invalid",
            "reason": "bundle 공개키가 운영자 보관본과 다름",
            "fingerprints": mismatched,
        }
    return {"status": "valid", "reason": "운영자 보관 공개키로 검증"}


def _untrusted(
    direction: str, receipt: archive_files.Receipt, reason: str
) -> dict[str, Any]:
    return {
        "direction": direction,
        "result": "invalid",
        "archive_file": {
            "uri": receipt.archive_file,
            "status": "invalid",
            "reason": reason,
        },
    }


def _check_transfer(
    objects: Sequence[copier.CopiedObject],
    read_target: Callable[[str], bytes | None],
) -> list[dict[str, str]]:
    results = []
    for item in objects:
        data = read_target(item.target_uri)
        if data is None:
            status = "missing"
        elif hashlib.sha256(data).hexdigest() == item.sha256:
            status = "valid"
        else:
            status = "invalid"
        results.append(
            {
                "source": item.source_uri,
                "target": item.target_uri,
                "status": status,
            }
        )
    return results


def _target_reader(
    target: object_store.ObjectStore,
) -> Callable[[str], bytes | None]:
    """대상 버킷 경로로 원본 바이트를 읽는다. 버킷 밖 경로는 없음으로 본다."""
    root = target.uri("")

    def read(uri: str) -> bytes | None:
        if not uri.startswith(root):
            return None
        stored = target.get(uri.removeprefix(root))
        return None if stored is None else stored.data

    return read


def _all_valid(transfer: list[dict[str, str]]) -> bool:
    return all(entry["status"] == "valid" for entry in transfer)


def _run_prefix(run_id: str) -> str:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError(f"run_id 는 영문 소문자·숫자 1–16자: {run_id!r}")
    return f"ccx-{run_id}/"


def _put_json(
    target: object_store.ObjectStore, key: str, document: dict[str, Any]
) -> str:
    """JSON 문서를 쓰고, 쓴 바이트의 SHA-256 을 돌려준다."""
    data = json.dumps(document, ensure_ascii=False, indent=2).encode()
    target.put(key, object_store.StoredObject(data=data, content_type=_JSON))
    return hashlib.sha256(data).hexdigest()
