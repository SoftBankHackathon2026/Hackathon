"""Secret 저장소 계약 — AWS Secrets Manager 와 GCP Secret Manager 공통.

값은 bytes 로만 오간다. 구현은 클라이언트 예외를 SecretStoreError 로 바꿔
던지며, 원래 예외를 잇지 않는다(`from None`). 클라이언트 오류 메시지가 요청
인자(값)를 담는 경우가 있어서다(botocore ParamValidationError).
"""

import dataclasses
import logging
import uuid
from typing import Literal, Protocol

RetireOutcome = Literal["retired", "kept", "left_current"]

_TOKEN_NAMESPACE = uuid.UUID("5b0f4c6e-2f1a-4b8e-9a51-6c1d0e7a3f20")

# DEBUG 에서 요청·응답 본문(비밀값)을 로그로 남길 수 있는 SDK 로거 트리.
# 하위 로거(botocore.parsers 등)만 DEBUG 여도 값이 찍힌다.
_SDK_LOGGERS = ("botocore", "boto3", "google", "urllib3", "psycopg")


class UnsafeLoggingError(Exception):
    """SDK 로거가 DEBUG 라 비밀값이 로그에 남을 수 있다."""


def refuse_sdk_debug_logging() -> None:
    """SDK 로거 트리 어디든 실효 수준이 DEBUG 면 거부한다.

    비밀값을 읽거나 쓰기 전에 부른다. 루트 로거가 DEBUG 여도 걸린다.

    Raises:
        UnsafeLoggingError: DEBUG 가 켜진 로거 이름을 담아.
    """
    noisy = [
        name
        for name in _sdk_logger_names()
        if logging.getLogger(name).isEnabledFor(logging.DEBUG)
    ]
    if noisy:
        raise UnsafeLoggingError(
            "DEBUG 로그가 켜진 SDK 로거: " + ", ".join(noisy)
        )


def _sdk_logger_names() -> list[str]:
    existing = [
        name
        for name, logger in logging.root.manager.loggerDict.items()
        if isinstance(logger, logging.Logger)
        and any(name.startswith(p + ".") for p in _SDK_LOGGERS)
    ]
    return [*_SDK_LOGGERS, *sorted(existing)]


def version_token(run_id: str, key: str) -> str:
    """실행·대상마다 고정된 쓰기 토큰. 재시도가 같은 버전을 가리키게 한다.

    AWS ClientRequestToken(32~64자)으로 쓴다. 키는 보통 대상 Secret 식별자다.
    """
    return str(uuid.uuid5(_TOKEN_NAMESPACE, f"{run_id}/{key}"))


@dataclasses.dataclass(frozen=True)
class WrittenVersion:
    """이 실행이 대상 Secret 에 쓴(또는 이미 있던) 버전. 값은 담지 않는다.

    Attributes:
        secret_id: 대상 Secret 식별자.
        version_id: 버전 식별자(AWS VersionId, GCP 버전 리소스 이름).
        created: 이 실행이 만든 버전이면 True. 되돌리기는 이것만 폐기한다.
        prior_labels: 쓰기 전 스테이지 레이블 → 버전(AWS 만). 되돌릴 때
            레이블을 원래 자리로 옮기는 데 쓴다. GCP 는 빈 tuple.
    """

    secret_id: str
    version_id: str
    created: bool
    prior_labels: tuple[tuple[str, str], ...]


class SecretStoreError(Exception):
    """저장소 호출 실패. 메시지는 동작·Secret 식별자·오류 코드뿐이다."""

    def __init__(self, operation: str, secret_id: str, reason: str) -> None:
        """Args: 실패한 동작, 대상 Secret 식별자, 클라이언트 오류 코드."""
        super().__init__(f"{operation} 실패: {secret_id} ({reason})")
        self.operation = operation
        self.secret_id = secret_id
        self.reason = reason


class SecretStore(Protocol):
    """Secret 서비스 하나(한 계정·한 프로젝트)."""

    def read_current(self, secret_id: str) -> bytes | None:
        """현재 값(AWSCURRENT·latest). 버전이 하나도 없으면 None."""
        ...

    def read_version(self, secret_id: str, version_id: str) -> bytes:
        """특정 버전의 값. 쓰기 뒤 대조에 쓴다."""
        ...

    def add_version(
        self, secret_id: str, value: bytes, token: str
    ) -> WrittenVersion:
        """새 버전을 쓴다. 같은 token 의 재시도는 새 버전을 만들지 않는다."""
        ...

    def retire_version(self, written: WrittenVersion) -> RetireOutcome:
        """이 실행이 만든 버전을 폐기한다. created 가 False 면 손대지 않는다."""
        ...
