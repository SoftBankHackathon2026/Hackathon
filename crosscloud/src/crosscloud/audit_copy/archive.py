"""감사 기록 이관의 두 방향과, 이관한 결과의 검증.

대상 버킷 안 배치(run 하나 = 접두사 하나):

  ccx-<run>/objects/<원래 키>   원본 바이트 그대로
  ccx-<run>/bundle.json        AWS→GCP 검증 묶음
  ccx-<run>/manifest.json      GCP→AWS 해시 매니페스트

AWS→GCP 는 CloudTrail 로그·다이제스트를 옮기고 bundle.json 을 남긴다.
GCP→AWS 는 `gcloud logging copy` 가 GCS 에 쓴 결과 파일을 옮기고
manifest.json 을 남긴다. 검증은 대상 버킷만 읽는다(오프라인).
"""

import hashlib
import json
import re
from collections.abc import Callable, Sequence
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


def archive_cloudtrail(
    source: object_store.ObjectStore,
    source_prefix: str,
    target: object_store.ObjectStore,
    run_id: str,
    public_keys: Sequence[archive_files.PublicKey],
) -> dict[str, Any]:
    """AWS→GCP: trail 버킷의 로그·다이제스트를 옮기고 bundle.json 을 쓴다.

    Args:
        source: trail 버킷(원위치). 읽기만 한다.
        source_prefix: 옮길 키 접두사(예: "AWSLogs/<계정>/").
        target: 대상 보존 버킷.
        run_id: 이관 실행 식별자 run(ccx-<run> 의 run). 영문 소문자·숫자
            1–16자.
        public_keys: 해당 기간 ListPublicKeys 결과.

    Returns:
        대상에 쓴 bundle.json 문서.

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
    _put_json(target, prefix + "bundle.json", bundle)
    return bundle


def archive_gcp_export(
    source: object_store.ObjectStore,
    source_prefix: str,
    target: object_store.ObjectStore,
    run_id: str,
) -> dict[str, Any]:
    """GCP→AWS: logging copy 결과 파일을 옮기고 manifest.json 을 쓴다.

    Args:
        source: `gcloud logging copy` 가 쓴 GCS 버킷. 읽기만 한다.
        source_prefix: 옮길 키 접두사.
        target: 대상 S3 Object Lock 버킷.
        run_id: 이관 실행 식별자.

    Returns:
        대상에 쓴 manifest.json 문서(evidence: none, 한계 문구 포함).

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
    _put_json(target, prefix + "manifest.json", manifest)
    return manifest


def verify_cloudtrail_archive(
    target: object_store.ObjectStore, run_id: str
) -> dict[str, Any]:
    """옮긴 CloudTrail 기록을 대상 버킷만 읽어 검증한다.

    두 겹이다. transfer 는 객체마다 원본 바이트 SHA-256 이 bundle 과 같은지,
    cloudtrail 은 다이제스트 체인 서명과 로그 해시다.

    Returns:
        {"result", "transfer", "cloudtrail"} — result 는 둘 다 통과해야 valid.

    Raises:
        archive_files.InvalidArchiveFileError: bundle.json 이 없거나 형식이
            틀렸을 때.
    """
    prefix = _run_prefix(run_id)
    bundle = archive_files.bundle_from_json(
        _get_json(target, prefix + "bundle.json")
    )
    read_target = _target_reader(target)
    transfer = _check_transfer(bundle.objects, read_target)
    by_source = {item.source_uri: item.target_uri for item in bundle.objects}
    report = cloudtrail_verify.verify_cloudtrail(
        object_uris=tuple(by_source),
        signatures=bundle.signatures,
        public_keys=bundle.public_keys,
        read=lambda uri: read_target(by_source.get(uri, "")),
    )
    return {
        "direction": "aws_to_gcp",
        "result": _overall(transfer, report.ok),
        "transfer": transfer,
        "cloudtrail": cloudtrail_verify.report_to_dict(report),
    }


def verify_gcp_archive(
    target: object_store.ObjectStore, run_id: str
) -> dict[str, Any]:
    """옮긴 GCP 감사 기록을 manifest.json 해시로만 대조한다.

    공급자 서명이 없으므로 결과는 언제나 evidence: none 과 한계 문구를
    함께 싣는다.

    Raises:
        archive_files.InvalidArchiveFileError: manifest.json 이 없거나 형식이
            틀렸을 때.
    """
    prefix = _run_prefix(run_id)
    objects = archive_files.manifest_from_json(
        _get_json(target, prefix + "manifest.json")
    )
    transfer = _check_transfer(objects, _target_reader(target))
    return {
        "direction": "gcp_to_aws",
        "result": _overall(transfer, True),
        "evidence": "none",
        "limitation": archive_files.GCP_LIMITATION,
        "transfer": transfer,
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


def _overall(transfer: list[dict[str, str]], provider_ok: bool) -> str:
    transfer_ok = all(entry["status"] == "valid" for entry in transfer)
    return "valid" if transfer_ok and provider_ok else "invalid"


def _run_prefix(run_id: str) -> str:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError(f"run_id 는 영문 소문자·숫자 1–16자: {run_id!r}")
    return f"ccx-{run_id}/"


def _put_json(
    target: object_store.ObjectStore, key: str, document: dict[str, Any]
) -> None:
    data = json.dumps(document, ensure_ascii=False, indent=2).encode()
    target.put(key, object_store.StoredObject(data=data, content_type=_JSON))


def _get_json(target: object_store.ObjectStore, key: str) -> object:
    stored = target.get(key)
    if stored is None:
        raise archive_files.InvalidArchiveFileError(f"없음: {target.uri(key)}")
    try:
        return json.loads(stored.data)
    except ValueError as e:
        raise archive_files.InvalidArchiveFileError(
            f"JSON 이 아님: {target.uri(key)}"
        ) from e
