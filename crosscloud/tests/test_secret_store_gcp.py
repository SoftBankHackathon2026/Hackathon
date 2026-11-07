import traceback
import types

import google_crc32c
import pytest
from google.api_core import exceptions as google_exceptions

from crosscloud.secret_transfer import store, store_gcp

_CANARY = b"canary-gcp-9be0-should-not-leak"
_SECRET = "projects/ghost-test/secrets/ccx-s1-db-url"


def _payload(data: bytes, crc: int | None = None):
    checksum = google_crc32c.value(data) if crc is None else crc
    return types.SimpleNamespace(data=data, data_crc32c=checksum)


def _number(name: str) -> int:
    return int(name.rsplit("/", 1)[1])


class FakeSecretManager:
    """SecretManagerServiceClient 의 쓰는 메서드를 실제 계약대로 흉내 낸다.

    `latest` 는 상태와 무관하게 마지막에 만든 버전이다. 파기된 버전을
    읽으면 FailedPrecondition 이다(versions.access 문서).
    """

    def __init__(self):
        self.versions: dict[str, bytes] = {}
        self.destroyed: list[str] = []
        self.added: list[dict] = []
        self.listed: list[dict] = []
        self.corrupt_crc = False
        self.add_error: Exception | None = None
        self.access_error: Exception | None = None
        self.list_error: Exception | None = None

    def _names(self, secret: str) -> list[str]:
        prefix = secret + "/versions/"
        return sorted(
            (n for n in self.versions if n.startswith(prefix)), key=_number
        )

    def access_secret_version(self, request):
        if self.access_error is not None:
            raise self.access_error
        name = request["name"]
        secret, _, version = name.partition("/versions/")
        if version == "latest":
            names = self._names(secret)
            name = names[-1] if names else None
        if name is None or name not in self.versions:
            raise google_exceptions.NotFound("version not found")
        if name in self.destroyed:
            raise google_exceptions.FailedPrecondition("version is destroyed")
        data = self.versions[name]
        crc = 0 if self.corrupt_crc else None
        return types.SimpleNamespace(name=name, payload=_payload(data, crc))

    def list_secret_versions(self, request):
        self.listed.append(request)
        if self.list_error is not None:
            raise self.list_error
        assert request["filter"] == "state:ENABLED"
        # 실제 API 처럼 최신이 먼저 오지만, 순서에 기대지 않는지 보려고 섞는다.
        enabled = [
            n for n in self._names(request["parent"]) if n not in self.destroyed
        ]
        return [
            types.SimpleNamespace(name=n) for n in enabled[1:] + enabled[:1]
        ]

    def add_secret_version(self, request):
        if self.add_error is not None:
            raise self.add_error
        self.added.append(request)
        parent = request["parent"]
        name = f"{parent}/versions/{len(self._names(parent)) + 1}"
        self.versions[name] = request["payload"]["data"]
        return types.SimpleNamespace(name=name)

    def destroy_secret_version(self, request):
        self.destroyed.append(request["name"])
        return types.SimpleNamespace(name=request["name"])


def _store(fake):
    return store_gcp.GcpSecretStore(fake, project="ghost-test")


def test_read_current_reads_latest_payload():
    fake = FakeSecretManager()
    fake.versions[_SECRET + "/versions/1"] = _CANARY

    assert _store(fake).read_current("ccx-s1-db-url") == _CANARY


def test_read_current_returns_none_without_versions():
    assert _store(FakeSecretManager()).read_current("ccx-s1-db-url") is None


def test_read_rejects_checksum_mismatch_without_value():
    fake = FakeSecretManager()
    fake.versions[_SECRET + "/versions/1"] = _CANARY
    fake.corrupt_crc = True

    with pytest.raises(store.SecretStoreError) as excinfo:
        _store(fake).read_current("ccx-s1-db-url")

    assert excinfo.value.reason == "crc32c_mismatch"
    assert _CANARY.decode() not in str(excinfo.value)


def test_add_version_sends_payload_with_checksum():
    fake = FakeSecretManager()

    written = _store(fake).add_version("ccx-s1-db-url", _CANARY, "token")

    assert written == store.WrittenVersion(
        secret_id="ccx-s1-db-url",
        version_id=_SECRET + "/versions/1",
        created=True,
        prior_labels=(),
    )
    assert fake.added[0]["payload"]["data_crc32c"] == google_crc32c.value(
        _CANARY
    )


def test_add_version_skips_write_when_latest_already_equal():
    fake = FakeSecretManager()
    fake.versions[_SECRET + "/versions/1"] = _CANARY

    written = _store(fake).add_version("ccx-s1-db-url", _CANARY, "token")

    assert written.created is False
    assert written.version_id == _SECRET + "/versions/1"
    assert fake.added == []


def test_read_version_accepts_full_version_name():
    fake = FakeSecretManager()
    fake.versions[_SECRET + "/versions/3"] = b"v3"

    value = _store(fake).read_version("ccx-s1-db-url", _SECRET + "/versions/3")

    assert value == b"v3"


def test_retire_destroys_only_created_version():
    fake = FakeSecretManager()
    secrets = _store(fake)
    created = secrets.add_version("ccx-s1-db-url", b"new", "token")
    kept = store.WrittenVersion(
        secret_id="ccx-s1-db-url",
        version_id=_SECRET + "/versions/9",
        created=False,
        prior_labels=(),
    )

    assert secrets.retire_version(created) == "retired"
    assert secrets.retire_version(kept) == "kept"
    assert fake.destroyed == [_SECRET + "/versions/1"]


def test_api_errors_become_store_error_without_value():
    fake = FakeSecretManager()
    fake.add_error = google_exceptions.PermissionDenied(
        "denied " + _CANARY.decode()
    )

    with pytest.raises(store.SecretStoreError) as excinfo:
        _store(fake).add_version("ccx-s1-db-url", _CANARY, "token")

    assert excinfo.value.reason == "PermissionDenied"
    assert _CANARY.decode() not in "".join(
        traceback.format_exception(excinfo.value)
    )


def test_read_current_raises_access_errors():
    fake = FakeSecretManager()
    fake.versions[_SECRET + "/versions/1"] = b"v"
    fake.access_error = google_exceptions.PermissionDenied("denied")

    with pytest.raises(store.SecretStoreError, match="PermissionDenied"):
        _store(fake).read_current("ccx-s1-db-url")


def test_read_current_raises_list_errors():
    fake = FakeSecretManager()
    fake.list_error = google_exceptions.PermissionDenied("denied")

    with pytest.raises(store.SecretStoreError, match="PermissionDenied"):
        _store(fake).read_current("ccx-s1-db-url")


def test_full_secret_name_is_used_as_is():
    fake = FakeSecretManager()
    fake.versions[_SECRET + "/versions/1"] = b"v"

    assert _store(fake).read_current(_SECRET) == b"v"


def test_rollback_restores_previous_enabled_version_as_current():
    fake = FakeSecretManager()
    secrets = _store(fake)
    secrets.add_version("ccx-s1-db-url", b"before", "t1")
    written = secrets.add_version("ccx-s1-db-url", _CANARY, "t2")

    secrets.retire_version(written)

    assert secrets.read_current("ccx-s1-db-url") == b"before"


def test_add_after_rollback_writes_new_version():
    fake = FakeSecretManager()
    secrets = _store(fake)
    secrets.add_version("ccx-s1-db-url", b"before", "t1")
    secrets.retire_version(secrets.add_version("ccx-s1-db-url", _CANARY, "t2"))

    again = secrets.add_version("ccx-s1-db-url", _CANARY, "t3")

    assert again.created is True
    assert secrets.read_current("ccx-s1-db-url") == _CANARY


def test_read_current_is_none_when_every_version_is_destroyed():
    fake = FakeSecretManager()
    secrets = _store(fake)
    secrets.retire_version(secrets.add_version("ccx-s1-db-url", b"x", "t1"))

    assert secrets.read_current("ccx-s1-db-url") is None
