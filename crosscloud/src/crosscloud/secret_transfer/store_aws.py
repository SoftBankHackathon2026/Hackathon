"""AWS Secrets Manager 구현.

  secrets = store_aws.AwsSecretStore(boto3.client("secretsmanager"))

버전 의미(실측 2026-11-07): 버전은 하나씩 지울 수 없다. 새 값을 쓰면 새 버전이
AWSCURRENT 를 받고, 직전 AWSCURRENT 는 AWSPREVIOUS 를 받으며, 그 전
AWSPREVIOUS 는 레이블을 잃는다. 그래서 쓰기 전에 스테이지 맵을 기록하고,
되돌리기는 레이블을 원래 자리로 옮기는 것이다. 값의 파기는 Secret 삭제다.
ClientRequestToken 이 곧 VersionId 라 같은 토큰의 재시도는 새 버전을 만들지
않는다. 다만 같은 토큰 버전이 레이블 없이 남아 있으면(같은 run_id 로 되돌린 뒤
재실행) AWS 는 쓰기를 조용히 무시한다. 그래서 쓰기 전에 레이블 없는 버전까지
나열해 토큰이 현재 버전일 때만 재시도로 받고, 그 밖은 token_reused 로 거부한다.
"""

from typing import Any

from botocore import exceptions as botocore_exceptions

from crosscloud.secret_transfer import store

_CURRENT = "AWSCURRENT"
_PREVIOUS = "AWSPREVIOUS"


class AwsSecretStore:
    """boto3 secretsmanager 클라이언트 하나(한 계정·한 리전)."""

    def __init__(self, client: Any) -> None:
        """Args: client 는 boto3.client("secretsmanager")."""
        self._client = client

    def read_current(self, secret_id: str) -> bytes | None:
        """AWSCURRENT 값. 버전이 없으면(ResourceNotFound) None."""
        try:
            response = self._call(
                "read_current",
                secret_id,
                self._client.get_secret_value,
                SecretId=secret_id,
                VersionStage=_CURRENT,
            )
        except store.SecretStoreError as e:
            if e.reason == "ResourceNotFoundException":
                return None
            raise
        return _value_of(response)

    def read_version(self, secret_id: str, version_id: str) -> bytes:
        """VersionId 로 고정한 값."""
        response = self._call(
            "read_version",
            secret_id,
            self._client.get_secret_value,
            SecretId=secret_id,
            VersionId=version_id,
        )
        return _value_of(response)

    def add_version(
        self, secret_id: str, value: bytes, token: str
    ) -> store.WrittenVersion:
        """UTF-8 이면 SecretString, 아니면 SecretBinary 로 쓴다.

        ECS 의 Secret 주입은 SecretString 만 받으므로 문자열이 기본이다.
        """
        stages = self._stages(secret_id)
        if token in stages:
            if _CURRENT not in stages[token]:
                raise store.SecretStoreError(
                    "add_version", secret_id, "token_reused"
                )
            return _retried(secret_id, token, stages)
        self._call(
            "add_version",
            secret_id,
            self._client.put_secret_value,
            SecretId=secret_id,
            ClientRequestToken=token,
            **_payload(value),
        )
        return store.WrittenVersion(
            secret_id=secret_id,
            version_id=token,
            created=True,
            prior_labels=_labels(stages, (_CURRENT, _PREVIOUS)),
        )

    def _stages(self, secret_id: str) -> dict[str, list[str]]:
        """버전 → 스테이지 레이블. 레이블 없는(deprecated) 버전도 담는다."""
        stages: dict[str, list[str]] = {}
        page_token: dict[str, str] = {}
        while True:
            page = self._call(
                "list_versions",
                secret_id,
                self._client.list_secret_version_ids,
                SecretId=secret_id,
                IncludeDeprecated=True,
                **page_token,
            )
            for version in page.get("Versions", ()):
                stages[version["VersionId"]] = version.get("VersionStages", [])
            if "NextToken" not in page:
                return stages
            page_token = {"NextToken": page["NextToken"]}

    def retire_version(
        self, written: store.WrittenVersion
    ) -> store.RetireOutcome:
        """AWSCURRENT·AWSPREVIOUS 를 쓰기 전 버전으로 되돌린다.

        그릇의 첫 버전이었으면 AWSCURRENT 를 옮길 곳이 없어 그대로 둔다
        (left_current). 그 값은 Secret 삭제(해체)로만 사라진다.
        """
        if not written.created:
            return "kept"
        prior = dict(written.prior_labels)
        if _CURRENT not in prior:
            return "left_current"
        self._move(written, _CURRENT, prior[_CURRENT])
        # AWSCURRENT 를 옮기면 AWSPREVIOUS 가 자동으로 이 버전에 붙는다.
        self._move(written, _PREVIOUS, prior.get(_PREVIOUS))
        return "retired"

    def _move(
        self, written: store.WrittenVersion, stage: str, to: str | None
    ) -> None:
        target = {"MoveToVersionId": to} if to else {}
        self._call(
            "retire_version",
            written.secret_id,
            self._client.update_secret_version_stage,
            SecretId=written.secret_id,
            VersionStage=stage,
            RemoveFromVersionId=written.version_id,
            **target,
        )

    @staticmethod
    def _call(
        operation: str, secret_id: str, method: Any, **kwargs: Any
    ) -> dict[str, Any]:
        # 원래 예외를 잇지 않는다 — 인자 검사 오류는 요청 인자를 메시지에
        # 담는다.
        try:
            return method(**kwargs)
        except botocore_exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "ClientError")
            raise store.SecretStoreError(operation, secret_id, code) from None
        except botocore_exceptions.BotoCoreError as e:
            reason = type(e).__name__
            raise store.SecretStoreError(operation, secret_id, reason) from None


def _value_of(response: dict[str, Any]) -> bytes:
    if "SecretBinary" in response:
        return response["SecretBinary"]
    return response["SecretString"].encode("utf-8")


def _payload(value: bytes) -> dict[str, Any]:
    try:
        return {"SecretString": value.decode("utf-8")}
    except UnicodeDecodeError:
        return {"SecretBinary": value}


def _labels(
    stages: dict[str, list[str]], wanted: tuple[str, ...]
) -> tuple[tuple[str, str], ...]:
    found = {
        label: version
        for version, labels in stages.items()
        for label in labels
        if label in wanted
    }
    return tuple((label, found[label]) for label in wanted if label in found)


def _retried(
    secret_id: str, token: str, stages: dict[str, list[str]]
) -> store.WrittenVersion:
    # 응답을 잃고 다시 온 경우다. 이 버전이 AWSCURRENT 를 가져갔으므로 쓰기 전
    # AWSCURRENT 는 지금 AWSPREVIOUS 를 가진 버전이다. 그 전 AWSPREVIOUS 는
    # 이미 레이블을 잃어 알 수 없다.
    previous = dict(_labels(stages, (_PREVIOUS,))).get(_PREVIOUS)
    prior = ((_CURRENT, previous),) if previous else ()
    return store.WrittenVersion(
        secret_id=secret_id, version_id=token, created=True, prior_labels=prior
    )
