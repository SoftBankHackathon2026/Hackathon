"""원본 Secret 값을 대상 Secret 에 옮기고 SHA-256 으로 대조한다.

  results = run.transfer_secrets(
      items, allowed, aws_store, gcp_store, run_id, target_name_prefix="ccx-r1")
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
from collections.abc import Sequence
from typing import Literal

from crosscloud.provision import residue
from crosscloud.secret_transfer import store

_log = logging.getLogger(__name__)


RollbackOutcome = store.RetireOutcome | Literal["failed"]
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
    """옮길 Secret 한 쌍. 대상 그릇은 Terraform 이 미리 만든다.

    대상 그릇 이름은 두 클라우드 모두 `<실행 접두사>-<키>` 다(AWS
    modules/base storage.tf, GCP target_base main.tf). 대상 식별자는 그 짧은
    이름이나 전체 리소스 이름(GCP `projects/…/secrets/<이름>`, AWS Secret ARN)
    이다.
    """

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


def transfer_secrets(
    items: Sequence[SecretTransferItem],
    allowed_source_ids: frozenset[str],
    source: store.SecretStore,
    target: store.SecretStore,
    run_id: str,
    *,
    target_name_prefix: str,
) -> tuple[SecretTransferResult, ...]:
    """허용 목록 안의 Secret 만 옮기고, 하나씩 대조한다.

    Args:
        items: 옮길 쌍들.
        allowed_source_ids: 옮겨도 되는 원본 Secret 식별자. 밖의 것이 하나라도
            있으면 아무 값도 읽지 않고 멈춘다. 문자열을 잘못 넘기면 부분
            문자열 검사가 되므로 frozenset 만 받는다.
        source: 원본 클라우드 저장소.
        target: 대상 클라우드 저장소.
        run_id: 이 실행의 식별자. 쓰기 토큰의 재료다.
        target_name_prefix: 이 실행의 이름 접두사(`ccx-<run>`). 대상 그릇
            이름이 `접두사-` 로 시작하지 않으면 아무 값도 읽지 않고 멈춘다 —
            이 실행이 만들지 않은 남의 Secret 에 버전을 더하지 않는다.

    Returns:
        쌍마다 결과. 모두 verified 다.

    Raises:
        TypeError: allowed_source_ids 가 frozenset 이 아닐 때.
        ValueError: target_name_prefix 가 실행 접두사 형식이 아닐 때.
        SecretTransferError: 허용 목록 밖·접두사 밖 대상·안전하지 않은 로그
            설정·읽기·쓰기·대조 실패. 어떤 예외로 끝나든 이 실행이 쓴
            버전(대조 전 버전 포함)을 먼저 되돌린다.
    """
    if not isinstance(allowed_source_ids, frozenset):
        raise TypeError("allowed_source_ids 는 frozenset 이어야 한다")
    _refuse_unlisted(items, allowed_source_ids)
    _refuse_foreign_targets(items, target_name_prefix)
    try:
        store.refuse_sdk_debug_logging()
    except store.UnsafeLoggingError as e:
        raise SecretTransferError("unsafe_logging", str(e)) from None
    written: list[store.WrittenVersion] = []
    results: list[SecretTransferResult] = []
    completed = False
    try:
        for item in items:
            results.append(_transfer_one(item, source, target, run_id, written))
        completed = True
    finally:
        if not completed:
            _retire_all(written, target)
    return tuple(results)


def roll_back_transfer(
    results: Sequence[SecretTransferResult], target: store.SecretStore
) -> tuple[RollbackOutcome, ...]:
    """이 실행이 쓴 버전을 뒤에서부터 폐기한다.

    AWS 는 버전을 하나씩 지울 수 없어 스테이지 레이블을 원래 자리로 돌린다.
    값 자체의 파기는 Secret 을 통째로 지우는 해체(terraform destroy)가 한다.

    Returns:
        결과 순서대로 폐기 결과. 폐기 호출이 실패한 것은 "failed" 다(나머지는
        계속 되돌린다).
    """
    return _retire_all([r.written for r in results], target)


def _retire_all(
    written: Sequence[store.WrittenVersion], target: store.SecretStore
) -> tuple[RollbackOutcome, ...]:
    # 하나가 실패해도 나머지를 계속 되돌린다. 실패는 결과와 로그로 알린다.
    outcomes: list[RollbackOutcome] = []
    for version in reversed(written):
        try:
            outcome: RollbackOutcome = target.retire_version(version)
        except store.SecretStoreError as e:
            _log.error("되돌림 실패 %s: %s", version.secret_id, e.reason)
            outcome = "failed"
        _log.info(
            "되돌림 %s %s: %s", version.secret_id, version.version_id, outcome
        )
        outcomes.append(outcome)
    return tuple(reversed(outcomes))


def _refuse_unlisted(
    items: Sequence[SecretTransferItem], allowed: frozenset[str]
) -> None:
    unlisted = sorted(
        {i.source_secret_id for i in items if i.source_secret_id not in allowed}
    )
    if unlisted:
        raise SecretTransferError("not_allowed", ", ".join(unlisted))


def _refuse_foreign_targets(
    items: Sequence[SecretTransferItem], name_prefix: str
) -> None:
    residue.check_name_prefix(name_prefix)
    foreign = sorted(
        {
            i.target_secret_id
            for i in items
            if not _short_name(i.target_secret_id).startswith(f"{name_prefix}-")
        }
    )
    if foreign:
        raise SecretTransferError("not_allowed", ", ".join(foreign))


def _short_name(secret_id: str) -> str:
    # GCP 전체 이름은 마지막 조각, AWS ARN 은 `:secret:` 뒤(이름-무작위 6자).
    if secret_id.startswith("projects/"):
        return secret_id.rsplit("/", 1)[1]
    if secret_id.startswith("arn:"):
        return secret_id.partition(":secret:")[2]
    return secret_id


def _transfer_one(
    item: SecretTransferItem,
    source: store.SecretStore,
    target: store.SecretStore,
    run_id: str,
    written_so_far: list[store.WrittenVersion],
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
            store.version_token(run_id, item.target_secret_id),
        )
        # 대조 전에 등록한다 — 읽기가 실패해도 되돌리기 대상에 들어간다.
        written_so_far.append(written)
        copied = target.read_version(written.secret_id, written.version_id)
    except store.SecretStoreError as e:
        raise SecretTransferError("target_write_failed", str(e)) from None
    result = SecretTransferResult(
        item=item, written=written, verified=_same_digest(value, copied)
    )
    if not result.verified:
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
