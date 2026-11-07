from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from controlplane.providers.contracts import (
    ProviderConflict,
    ProviderRejected,
    ProviderUnavailable,
    SubmissionUncertain,
)

Payload = dict[str, Any]

CONFLICT_CODES = frozenset(
    {
        "ConflictException",
        "IdempotentParameterMismatchException",
        "PriorityInUse",
        "DuplicateTargetGroupName",
        "EntityAlreadyExists",
    }
)
REJECTED_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "InvalidParameterException",
        "InvalidRequestException",
        "ClientException",
        "ValidationError",
        "ClusterNotFoundException",
        "ServiceNotActiveException",
        "PlatformTaskDefinitionIncompatibilityException",
        "UnsupportedFeatureException",
    }
)
TRANSIENT_CODES = frozenset(
    {"Throttling", "ThrottlingException", "RequestLimitExceeded", "ServiceUnavailableException"}
)


def error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "Unknown")


def read_failure(exc: ClientError) -> ProviderUnavailable | ProviderRejected:
    code = error_code(exc)
    if code in TRANSIENT_CODES:
        return ProviderUnavailable("AWS 조회가 일시적으로 실패했습니다.")
    return ProviderRejected(f"AWS 조회가 거절됐습니다 ({code}).")


class AwsGateway:
    def __init__(self, client: Any) -> None:
        self._client = client

    def _read(self, method: str, **kwargs: Any) -> Payload:
        try:
            return getattr(self._client, method)(**kwargs)
        except ClientError as exc:
            raise read_failure(exc) from None
        except BotoCoreError:
            raise ProviderUnavailable("AWS 조회가 일시적으로 실패했습니다.") from None

    def _read_optional(self, method: str, missing: frozenset[str], **kwargs: Any) -> Payload | None:
        try:
            return getattr(self._client, method)(**kwargs)
        except ClientError as exc:
            if error_code(exc) in missing:
                return None
            raise read_failure(exc) from None
        except BotoCoreError:
            raise ProviderUnavailable("AWS 조회가 일시적으로 실패했습니다.") from None

    def _write(self, method: str, **kwargs: Any) -> Payload:
        return self._write_optional(method, frozenset(), **kwargs)

    def _write_optional(
        self, method: str, missing: frozenset[str], **kwargs: Any
    ) -> Payload | None:
        try:
            return getattr(self._client, method)(**kwargs)
        except ClientError as exc:
            code = error_code(exc)
            if code in missing:
                return None
            if code in CONFLICT_CODES:
                raise ProviderConflict(f"AWS 리소스 상태가 충돌합니다 ({code}).") from None
            if code in REJECTED_CODES:
                raise ProviderRejected(
                    f"AWS가 요청을 거절했습니다 ({code}). 권한·입력을 확인하세요."
                ) from None
            raise SubmissionUncertain("AWS 쓰기 결과 불명. 조회 후 판단하세요.") from None
        except BotoCoreError:
            raise SubmissionUncertain(
                "AWS 응답을 확인하지 못했습니다. 재호출 전 조회하세요."
            ) from None
