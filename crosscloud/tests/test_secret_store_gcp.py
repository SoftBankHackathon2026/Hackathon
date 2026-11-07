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


class FakeSecretManager:
    """SecretManagerServiceClient 의 쓰는 메서드만 흉내 낸다."""

    def __init__(self):
        self.versions: dict[str, bytes] = {}
        self.destroyed: list[str] = []
        self.added: list[dict] = []
        self.corrupt_crc = False
        self.add_error: Exception | None = None
        self.access_error: Exception | None = None

    def _latest(self, secret: str) -> str | None:
        names = [
            n
            for n in self.versions
            if n.startswith(secret + "/versions/") and n not in self.destroyed
        ]
        return max(names, default=None)

    def access_secret_version(self, request):
        if self.access_error is not None:
            raise self.access_error
        name = request["name"]
        secret, _, version = name.partition("/versions/")
        if version == "latest":
            name = self._latest(secret)
        if name is None or name not in self.versions or name in self.destroyed:
            raise google_exceptions.NotFound("version not found")
        data = self.versions[name]
        crc = 0 if self.corrupt_crc else None
        return types.SimpleNamespace(name=name, payload=_payload(data, crc))

    def add_secret_version(self, request):
        if self.add_error is not None:
            raise self.add_error
        self.added.append(request)
        parent = request["parent"]
        name = f"{parent}/versions/{len(self.versions) + 1}"
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


def test_read_current_raises_other_api_errors():
    fake = FakeSecretManager()
    fake.access_error = google_exceptions.PermissionDenied("denied")

    with pytest.raises(store.SecretStoreError, match="PermissionDenied"):
        _store(fake).read_current("ccx-s1-db-url")


def test_full_secret_name_is_used_as_is():
    fake = FakeSecretManager()
    fake.versions[_SECRET + "/versions/1"] = b"v"

    assert _store(fake).read_current(_SECRET) == b"v"
