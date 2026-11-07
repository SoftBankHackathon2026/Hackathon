import pytest

from .conftest import common, manage


class InitAPI:
    token = None

    def __init__(self, initialized=False):
        self.initialized = initialized
        self.init_count = 0

    def health(self):
        return {"initialized": self.initialized, "sealed": False}

    def call(self, method, path, body=None):
        if path == "auth/token/lookup-self":
            return {"data": {}}
        assert path == "sys/init"
        self.initialized = True
        self.init_count += 1
        return {"root_token": "sensitive-root", "recovery_keys_base64": ["recovery"]}


def test_initialize_once_and_do_not_expose_root(raw_config, tmp_path):
    api = InitAPI()
    m = manage.Manager(raw_config, tmp_path, api)
    report, changed = m.bootstrap()
    assert changed and "sensitive-root" not in str(report)
    assert m.bootstrap()[1] is False
    assert api.init_count == 1


def test_initialized_without_receipt_never_reinitializes(raw_config, tmp_path):
    api = InitAPI(initialized=True)
    m = manage.Manager(raw_config, tmp_path, api)
    with pytest.raises(common.BaoError, match="INITIALIZED_WITHOUT_BOOTSTRAP_RECEIPT"):
        m.bootstrap()
    assert api.init_count == 0


def test_interrupted_initialization_requires_recovery(raw_config, tmp_path):
    common.save_json(tmp_path / "init-attempt.json", {"attempted": True})
    api = InitAPI()
    with pytest.raises(common.BaoError, match="INITIALIZATION_OUTCOME_UNKNOWN"):
        manage.Manager(raw_config, tmp_path, api).bootstrap()
    assert api.init_count == 0


def test_bundle_excludes_root_token(raw_config, tmp_path):
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o400)
    bootstrap = tmp_path / "bootstrap.json"
    common.save_json(
        bootstrap,
        {"root_token": "sensitive-root", "recovery_keys_base64": ["recovery"]},
    )
    report = common.bundle(key, bootstrap, "test", "cluster")
    assert "root_token" not in report and "sensitive-root" not in str(report)
