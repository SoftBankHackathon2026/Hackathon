from migration_connection.platform_bridge.factory import build


class Releases:
    def connect(self):
        raise AssertionError("factory must not connect")


def enable(monkeypatch, tmp_path, token):
    path = tmp_path / "runtime-token"
    path.write_text(token)
    path.chmod(0o600)
    monkeypatch.setenv("ONEPORT_CONNECTION_ENABLED", "true")
    monkeypatch.setenv("ONEPORT_CONNECTION_RUNTIME_TOKEN_FILE", str(path))
    monkeypatch.setenv("ONEPORT_CONNECTION_REDIS_URL", "redis://127.0.0.1:6399/0")


def test_empty_runtime_token_keeps_connections_disabled(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path, " \n")
    assert build(Releases()) is None


def test_runtime_token_is_read_without_surrounding_whitespace(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path, "  runtime-secret\n")
    service = build(Releases())
    assert service is not None
    assert service.runtime_token == "runtime-secret"


def test_group_or_world_readable_token_file_is_refused(monkeypatch, tmp_path):
    enable(monkeypatch, tmp_path, "runtime-secret")
    (tmp_path / "runtime-token").chmod(0o640)
    assert build(Releases()) is None
