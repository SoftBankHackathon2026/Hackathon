"""GCP Secret Manager 구현.

  secrets = store_gcp.GcpSecretStore(
      secretmanager.SecretManagerServiceClient(), project="ghost-project")

버전은 불변이다. `latest` 별칭은 상태와 무관하게 마지막에 만든 버전을
가리킨다 — 그 버전을 파기하면 `latest` 읽기는 FailedPrecondition 이 된다. 그래서
"현재 값"은 `latest` 가 아니라 활성(ENABLED) 버전 중 가장 최근 것으로 정한다
(목록 조회라 secretmanager.versions.list 권한이 필요하다). 되돌리기는 이 실행이
만든 버전을 파기(destroy)하는 것이고, 그러면 앞 활성 버전이 현재 값이 된다.
쓰기에 멱등 토큰이 없어서, 현재 값이 이미 같으면 쓰지 않는다. 오가는
페이로드는 CRC32C 로 확인한다.
"""

from typing import Any

import google_crc32c
from google.api_core import exceptions as google_exceptions

from crosscloud.secret_transfer import store


class GcpSecretStore:
    """SecretManagerServiceClient 하나와 프로젝트 하나."""

    def __init__(self, client: Any, project: str) -> None:
        """Args: SecretManagerServiceClient 와 프로젝트 ID."""
        self._client = client
        self._project = project

    def read_current(self, secret_id: str) -> bytes | None:
        """활성 버전 중 가장 최근 값. 활성 버전이 없으면 None."""
        current = self._current_version(secret_id)
        return None if current is None else current[1]

    def read_version(self, secret_id: str, version_id: str) -> bytes:
        """버전 리소스 이름(…/versions/N)으로 고정한 값."""
        return self._access(secret_id, version_id)[1]

    def add_version(
        self, secret_id: str, value: bytes, token: str
    ) -> store.WrittenVersion:
        """현재 값과 다를 때만 새 버전을 쓴다(token 은 쓰지 않는다)."""
        del token  # GCP 는 쓰기 멱등 토큰이 없다. 대신 같은 값이면 건너뛴다.
        current = self._current_version(secret_id)
        if current is not None and current[1] == value:
            return _written(secret_id, current[0], created=False)
        version = self._call(
            "add_version",
            secret_id,
            self._client.add_secret_version,
            request={
                "parent": self._secret_name(secret_id),
                "payload": {
                    "data": value,
                    "data_crc32c": google_crc32c.value(value),
                },
            },
        )
        return _written(secret_id, version.name, created=True)

    def retire_version(
        self, written: store.WrittenVersion
    ) -> store.RetireOutcome:
        """이 실행이 만든 버전을 파기한다. 값은 복구할 수 없게 지워진다."""
        if not written.created:
            return "kept"
        self._call(
            "retire_version",
            written.secret_id,
            self._client.destroy_secret_version,
            request={"name": written.version_id},
        )
        return "retired"

    def _current_version(self, secret_id: str) -> tuple[str, bytes] | None:
        versions = self._call(
            "list_versions",
            secret_id,
            self._client.list_secret_versions,
            request={
                "parent": self._secret_name(secret_id),
                "filter": "state:ENABLED",
            },
        )
        # 페이지 반복도 API 호출이라 같은 오류 변환 안에서 이름을 모은다.
        names = self._call(
            "list_versions", secret_id, _names_of, versions=versions
        )
        if not names:
            return None
        newest = max(names, key=lambda name: int(name.rsplit("/", 1)[1]))
        return self._access(secret_id, newest)

    def _access(self, secret_id: str, version_name: str) -> tuple[str, bytes]:
        response = self._call(
            "read",
            secret_id,
            self._client.access_secret_version,
            request={"name": version_name},
        )
        data = response.payload.data
        if google_crc32c.value(data) != response.payload.data_crc32c:
            raise store.SecretStoreError("read", secret_id, "crc32c_mismatch")
        return response.name, data

    def _secret_name(self, secret_id: str) -> str:
        if secret_id.startswith("projects/"):
            return secret_id
        return f"projects/{self._project}/secrets/{secret_id}"

    @staticmethod
    def _call(
        operation: str, secret_id: str, method: Any, **kwargs: Any
    ) -> Any:
        # 원래 예외를 잇지 않는다 — 서버 메시지를 그대로 믿지 않는다.
        try:
            return method(**kwargs)
        except google_exceptions.GoogleAPIError as e:
            reason = type(e).__name__
            raise store.SecretStoreError(operation, secret_id, reason) from None


def _names_of(versions: Any) -> list[str]:
    return [version.name for version in versions]


def _written(
    secret_id: str, version_id: str, *, created: bool
) -> store.WrittenVersion:
    return store.WrittenVersion(
        secret_id=secret_id,
        version_id=version_id,
        created=created,
        prior_labels=(),
    )
