"""보안 실측 수집의 공통 부분: 오류 분류, 필드별 보호, 환경 단위 수집, 시뮬레이터·테스트용 리더.

클라우드별 구현은 cloud_security_aws.py(boto3, aws 추가 의존성)와 cloud_security_gcp.py
(google-auth + httpx, 기본 의존성)에 있다. 모두 읽기 전용 API 만 부른다.
"""

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TypeVar

from controlplane.domain.cloud_security import (
    OBSERVED_FACTS,
    CloudSecurityFacts,
    CloudSecurityReader,
    Failure,
    Observation,
    every_fact_failed,
)
from controlplane.domain.fleet import EnvironmentConfig

LOG = logging.getLogger(__name__)
T = TypeVar("T")

# AWS 오류 코드 중 권한 부족으로 보는 것. "AccessDenied" 가 들어간 코드도 권한 부족이다.
_DENIED_CODES = {
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "UnauthorizedAccess",
    "AuthorizationError",
    "Forbidden",
}
# 자격 증명 자체가 없거나 만료된 경우. 권한 부족과 달리 수집기 설정 문제다.
_CREDENTIAL_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
}
_CREDENTIAL_ERRORS = {
    "NoCredentialsError",
    "PartialCredentialsError",
    "TokenRetrievalError",
    "SSOTokenLoadError",
    "UnauthorizedSSOTokenError",
    "DefaultCredentialsError",
    "RefreshError",
}
_TIMEOUT_ERRORS = {
    "ConnectTimeoutError",
    "ReadTimeoutError",
    "EndpointConnectionError",
    "ConnectTimeout",
    "ReadTimeout",
    "TimeoutException",
    "TimeoutError",
}


def _names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


def _gcp_reason(response: object) -> str | None:
    """GCP 오류 본문의 ErrorInfo.reason (예: SERVICE_DISABLED). 읽지 못하면 None."""
    try:
        details = response.json()["error"].get("details") or []  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 본문이 JSON 이 아니면 이유 없이 상태 코드로만 분류한다
        return None
    return next((d.get("reason") for d in details if d.get("reason")), None)


def classify(exc: BaseException, action: str) -> tuple[Failure, str]:
    """예외를 실패 종류와 화면에 보일 사유로 바꾼다. botocore·httpx 를 import 하지 않고 이름으로 본다."""
    names = _names(exc)
    response = getattr(exc, "response", None)
    if isinstance(response, dict) and "Error" in response:
        code = str(response["Error"].get("Code") or "")
        if code in _DENIED_CODES or "AccessDenied" in code:
            return "denied", action
        if code in _CREDENTIAL_CODES:
            return "no_credentials", code
        if code.endswith("NotFound") or code.endswith("NotFoundFault") or "NotFound" in code:
            return "not_found", code
        return "error", code or type(exc).__name__
    status = getattr(response, "status_code", None)
    if status == 403 and _gcp_reason(response) == "SERVICE_DISABLED":
        return "api_disabled", f"{action} (SERVICE_DISABLED)"
    if status in (401, 403):
        return "denied", f"{action} (HTTP {status})"
    if status == 404:
        return "not_found", f"{action} (HTTP 404)"
    if names & _CREDENTIAL_ERRORS:
        return "no_credentials", type(exc).__name__
    if names & _TIMEOUT_ERRORS:
        return "timeout", action
    if status is not None:
        return "error", f"{action} (HTTP {status})"
    return "error", type(exc).__name__


def attempt(action: str, read: Callable[[], T]) -> tuple[T | None, Observation | None]:
    """값을 읽어 (값, None) 을, 실패하면 (None, 실패 Observation) 을 돌려준다."""
    try:
        return read(), None
    except Exception as exc:  # noqa: BLE001 - 호출 하나의 오류를 필드 실패로 바꾼다
        failure, evidence = classify(exc, action)
        return None, Observation.failed(failure, evidence)


def guarded(action: str, read: Callable[[], Observation]) -> Observation:
    """한 필드 읽기. 실패해도 예외를 밖으로 내보내지 않고 실패 Observation 으로 바꾼다."""
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 - 한 필드의 오류가 다른 필드 수집을 막지 않게 한다
        failure, evidence = classify(exc, action)
        return Observation.failed(failure, evidence)


def collect_cloud_security(
    env: EnvironmentConfig,
    readers: Mapping[str, CloudSecurityReader],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CloudSecurityFacts | None:
    """환경 하나의 실측. 온프렘은 None(신고값 사용). 리더가 예외를 던지면 모든 필드를 실패로 채운다."""
    provider = env.provider
    if provider not in ("aws", "gcp"):
        return None
    if env.cloud_scope is None:
        return every_fact_failed(
            provider, now(), "unconfigured", "fleet.json 환경에 cloud_scope 가 없음"
        )
    reader = readers.get(provider)
    if reader is None:
        return every_fact_failed(provider, now(), "unconfigured", f"{provider} 수집기 없음")
    try:
        facts = reader.read(env)
    except Exception as exc:  # noqa: BLE001 - 수집기 전체 실패도 신고값 폴백으로 처리한다
        failure, evidence = classify(exc, f"{provider} 보안 실측")
        LOG.warning("env=%s cloud security error=%s", env.id, type(exc).__name__)
        return every_fact_failed(provider, now(), failure, evidence)
    missing = {
        fact: Observation.failed("error", "수집기가 값을 돌려주지 않음")
        for fact in OBSERVED_FACTS
        if fact not in facts.facts
    }
    return facts.model_copy(update={"facts": {**facts.facts, **missing}})


def default_readers() -> dict[str, CloudSecurityReader]:
    """운영 수집기. boto3 가 없으면 AWS 리더 대신 실패를 돌려주는 리더를 쓴다."""
    from controlplane.integrations.cloud_security_gcp import GcpSecurityReader

    readers: dict[str, CloudSecurityReader] = {"gcp": GcpSecurityReader()}
    try:
        from controlplane.integrations.cloud_security_aws import AwsSecurityReader

        readers["aws"] = AwsSecurityReader()
    except ImportError as exc:
        readers["aws"] = _Unavailable(exc)
    return readers


class _Unavailable:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def read(self, env: EnvironmentConfig) -> CloudSecurityFacts:
        raise self._error


class StaticCloudSecurityReader:
    """시뮬레이터·테스트용. 정해 둔 Observation 을 그대로 돌려준다."""

    def __init__(
        self,
        facts: Mapping[str, Observation],
        provider: str = "aws",
        at: datetime | None = None,
    ) -> None:
        self._facts = dict(facts)
        self._provider = provider
        self._at = at

    def read(self, env: EnvironmentConfig) -> CloudSecurityFacts:
        return CloudSecurityFacts(
            observed_at=self._at or datetime.now(UTC),
            provider=env.provider if env.provider in ("aws", "gcp") else self._provider,
            facts=self._facts,
        )
