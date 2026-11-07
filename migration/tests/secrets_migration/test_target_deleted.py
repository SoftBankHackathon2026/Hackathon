import io
import json
from urllib.error import HTTPError

import pytest
from migration.secrets.common import SecretsError
from migration.secrets.target import Http, deleted_version


def error(body):
    return HTTPError(
        "https://bao.test/v1/kv/data/x", 404, "not found", {}, io.BytesIO(body)
    )


def test_deleted_or_destroyed_latest_version_is_recognised():
    deleted = {
        "data": {
            "data": None,
            "metadata": {"deletion_time": "2026-11-07T00:00:00Z", "version": 3},
        }
    }
    destroyed = {
        "data": {"data": None, "metadata": {"deletion_time": "", "destroyed": True}}
    }
    assert deleted_version(error(json.dumps(deleted).encode())) is True
    assert deleted_version(error(json.dumps(destroyed).encode())) is True


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b'{"errors": []}',
        b'{"data": {"metadata": "x"}}',
        b'{"data": {"metadata": {"deletion_time": ""}}}',
        b'{"data": null}',
    ],
)
def test_plain_missing_paths_stay_not_found(body):
    assert deleted_version(error(body)) is False


def test_http_maps_deleted_versions_to_their_own_code(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("t")
    token.chmod(0o600)
    ca = tmp_path / "ca.pem"
    ca.write_text("")
    monkeypatch.setattr(
        "migration.secrets.target.ssl.create_default_context", lambda cafile: None
    )
    http = Http("https://bao.test", str(ca), "file:" + str(token))
    body = json.dumps(
        {"data": {"metadata": {"deletion_time": "2026-11-07T00:00:00Z"}}}
    ).encode()

    class Opener:
        def __init__(self, body):
            self.body = body

        def open(self, request, timeout):
            raise error(self.body)

    http.opener = Opener(body)
    with pytest.raises(SecretsError, match="TARGET_VERSION_DELETED"):
        http.request("GET", "kv/data/x")
    http.opener = Opener(b"{}")
    with pytest.raises(SecretsError, match="TARGET_NOT_FOUND"):
        http.request("GET", "kv/data/x")
