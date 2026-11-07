"""원본 Secret 값을 대상 Secret 에 옮기고 SHA-256 으로 대조한다.

  results = run.transfer_secrets(items, allowed, aws_store, gcp_store, run_id)
  ...
  run.roll_back_transfer(results, gcp_store)   # 이 실행이 쓴 버전만 폐기

방향(AWS→GCP·GCP→AWS)은 어느 저장소를 source·target 으로 넘기느냐로 정한다.
전부 옮기고 대조하거나, 하나라도 실패하면 이 실행이 쓴 버전을 되돌리고
SecretTransferError 를 던진다. 값·해시는 결과·로그·예외 어디에도 없다.
"""

import dataclasses
import hashlib
import hmac
import logging
import uuid
from collections.abc import Collection, Sequence
from typing import Literal

from crosscloud.secret_transfer import store

_log = logging.getLogger(__name__)

# 클라이언트 라이브러리가 DEBUG 에서 요청 본문(값)을 로그로 남길 수 있는
# 로거들. 하나라도 DEBUG 가 켜져 있으면 값을 읽기 전에 멈춘다.
_REQUEST_LOGGERS = ("botocore", "boto3", "google", "urllib3")
_TOKEN_NAMESPACE = uuid.UUID("5b0f4c6e-2f1a-4b8e-9a51-6c1d0e7a3f20")

ErrorClass = Literal[
    "not_allowed",
    "unsafe_logging",
    "source_missing",
    "source_read_failed",
    "target_write_failed",
    "mismatch",
]


@dataclasses.dataclass(frozen=True)
class SecretTransferItem:
    """옮길 Secret 한 쌍. 대상 그릇은 Terraform 이 미리 만든다."""

    source_secret_id: str
    target_secret_id: str


@dataclasses.dataclass(frozen=True)
class SecretTransferResult:
    """한 쌍을 옮긴 결과. 값·해시는 담지 않는다.

    Attributes:
        item: 옮긴 쌍.
        written: 대상에 쓴 버전.
        verified: 원본 값과 대상 버전 값의 SHA-256 이 같았는지.
    """

    item: SecretTransferItem
    written: store.WrittenVersion
    verified: bool


class SecretTransferError(Exception):
    """이관 실패. 메시지는 분류와 Secret 식별자뿐이다."""

    def __init__(self, error_class: ErrorClass, detail: str) -> None:
        """Args: 실패 분류, 값이 없는 설명(식별자·오류 코드)."""
        super().__init__(f"{error_class}: {detail}")
        self.error_class = error_class


def version_token(run_id: str, key: str) -> str:
    """실행·대상마다 고정된 쓰기 토큰. 재시도가 같은 버전을 가리키게 한다."""
    return str(uuid.uuid5(_TOKEN_NAMESPACE, f"{run_id}/{key}"))


def transfer_secrets(
    items: Sequence[SecretTransferItem],
    allowed_source_ids: Collection[str],
    source: store.SecretStore,
    target: store.SecretStore,
    run_id: str,
) -> tuple[SecretTransferResult, ...]:
    """허용 목록 안의 Secret 만 옮기고, 하나씩 대조한다.

    Args:
        items: 옮길 쌍들.
        allowed_source_ids: 옮겨도 되는 원본 Secret 식별자. 밖의 것이 하나라도
            있으면 아무 값도 읽지 않고 멈춘다.
        source: 원본 클라우드 저장소.
        target: 대상 클라우드 저장소.
        run_id: 이 실행의 식별자. 쓰기 토큰의 재료다.

    Returns:
        쌍마다 결과. 모두 verified 다.

    Raises:
        SecretTransferError: 허용 목록 밖·안전하지 않은 로그 설정·읽기·쓰기·
            대조 실패. 쓰기 이후의 실패면 이 실행이 쓴 버전을 먼저 되돌린다.
    """
    _refuse_unlisted(items, allowed_source_ids)
    _refuse_request_debug_logging()
    results: list[SecretTransferResult] = []
    try:
        for item in items:
            results.append(_transfer_one(item, source, target, run_id))
    except SecretTransferError:
        roll_back_transfer(results, target)
        raise
    return tuple(results)


def roll_back_transfer(
    results: Sequence[SecretTransferResult], target: store.SecretStore
) -> tuple[store.RetireOutcome, ...]:
    """이 실행이 쓴 버전을 뒤에서부터 폐기한다.

    AWS 는 버전을 하나씩 지울 수 없어 스테이지 레이블을 원래 자리로 돌린다.
    값 자체의 파기는 Secret 을 통째로 지우는 해체(terraform destroy)가 한다.

    Returns:
        결과 순서대로 폐기 결과.
    """
    outcomes = [target.retire_version(r.written) for r in reversed(results)]
    for result, outcome in zip(reversed(results), outcomes, strict=True):
        _log.info(
            "되돌림 %s → %s: %s",
            result.item.source_secret_id,
            result.item.target_secret_id,
            outcome,
        )
    return tuple(reversed(outcomes))


def _refuse_unlisted(
    items: Sequence[SecretTransferItem], allowed: Collection[str]
) -> None:
    unlisted = sorted(
        {i.source_secret_id for i in items if i.source_secret_id not in allowed}
    )
    if unlisted:
        raise SecretTransferError("not_allowed", ", ".join(unlisted))


def _refuse_request_debug_logging() -> None:
    noisy = [
        name
        for name in _REQUEST_LOGGERS
        if logging.getLogger(name).isEnabledFor(logging.DEBUG)
    ]
    if noisy:
        raise SecretTransferError(
            "unsafe_logging",
            "DEBUG 로그가 켜진 클라이언트: " + ", ".join(noisy),
        )


def _transfer_one(
    item: SecretTransferItem,
    source: store.SecretStore,
    target: store.SecretStore,
    run_id: str,
) -> SecretTransferResult:
    try:
        value = source.read_current(item.source_secret_id)
    except store.SecretStoreError as e:
        raise SecretTransferError("source_read_failed", str(e)) from None
    if value is None:
        raise SecretTransferError("source_missing", item.source_secret_id)
    try:
        written = target.add_version(
            item.target_secret_id,
            value,
            version_token(run_id, item.target_secret_id),
        )
        copied = target.read_version(written.secret_id, written.version_id)
    except store.SecretStoreError as e:
        raise SecretTransferError("target_write_failed", str(e)) from None
    result = SecretTransferResult(
        item=item, written=written, verified=_same_digest(value, copied)
    )
    if not result.verified:
        if written.created:
            target.retire_version(written)
        raise SecretTransferError("mismatch", item.target_secret_id)
    _log.info(
        "이관 %s → %s 버전 %s 대조 일치",
        item.source_secret_id,
        item.target_secret_id,
        written.version_id,
    )
    return result


def _same_digest(left: bytes, right: bytes) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(left).digest(), hashlib.sha256(right).digest()
    )
