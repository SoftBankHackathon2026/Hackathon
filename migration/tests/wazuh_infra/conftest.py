import importlib.util
import sys

import pytest

from migration.wazuh_infra.config import ASSETS


@pytest.fixture
def raw_config(tmp_path):
    for name in ("kubeconfig", "ssh-key", "known_hosts", "operator", "reader"):
        p = tmp_path / name
        p.write_text("test-password-with-32-characters-" + name)
        p.chmod(0o600)
    return {
        "schema_version": 1,
        "request_id": "request-1",
        "cluster": {
            "id": "onprem",
            "kubeconfig": str(tmp_path / "kubeconfig"),
            "context": "test",
        },
        "nodes": [
            {
                "name": "worker-1",
                "uid": "uid-1",
                "ssh_host": "192.0.2.1",
                "ssh_user": "ubuntu",
                "private_key_file": str(tmp_path / "ssh-key"),
                "known_hosts_file": str(tmp_path / "known_hosts"),
            }
        ],
        "target": {
            "installation_id": "audit-1",
            "node_name": "worker-1",
            "bind_address": "192.0.2.1",
        },
        "access": {
            "operator_password_ref": str(tmp_path / "operator"),
            "reader_password_ref": str(tmp_path / "reader"),
        },
    }


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def remote_modules():
    pytest.importorskip("ansible")
    common = module_at(
        "ansible.module_utils.wazuh_common", ASSETS / "module_utils/wazuh_common.py"
    )
    helpers = module_at(
        "ansible.module_utils.wazuh_host_helpers",
        ASSETS / "module_utils/wazuh_host_helpers.py",
    )
    module_at(
        "ansible.module_utils.wazuh_ingestion",
        ASSETS / "module_utils/wazuh_ingestion.py",
    )
    material = module_at(
        "ansible.module_utils.wazuh_material", ASSETS / "module_utils/wazuh_material.py"
    )
    host = module_at("wi_host_test", ASSETS / "library/wazuh_host.py")
    return common, helpers, material, host


@pytest.fixture
def remote_manage(remote_modules):
    return module_at("wi_manage_control_test", ASSETS / "library/wazuh_manage.py")
