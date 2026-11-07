import importlib.util
import sys

import pytest

from migration.openbao_infra.config import ASSETS


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if importlib.util.find_spec("ansible"):
    common = module_at(
        "ansible.module_utils.bao_common", ASSETS / "module_utils/bao_common.py"
    )
    manage = module_at("openbao_test_manage", ASSETS / "library/openbao_manage.py")
    material = module_at(
        "openbao_test_material", ASSETS / "library/openbao_material.py"
    )
    keycloak = module_at(
        "openbao_test_keycloak", ASSETS / "library/openbao_keycloak.py"
    )
else:
    common = manage = material = keycloak = None


@pytest.fixture(autouse=True)
def require_optional_infrastructure_dependencies():
    pytest.importorskip(
        "ansible", reason="install migration[openbao-infra] for infrastructure tests"
    )
    pytest.importorskip(
        "cryptography",
        reason="install migration[openbao-infra] for infrastructure tests",
    )


@pytest.fixture
def raw_config(tmp_path):
    for name in ("kubeconfig", "ssh", "known_hosts"):
        (tmp_path / name).write_text("fixture")
        (tmp_path / name).chmod(0o600)
    directory = tmp_path / "handoff"
    directory.mkdir(mode=0o700)
    return {
        "schema_version": 1,
        "request_id": "request-1",
        "cluster": {
            "id": "cluster-1",
            "kubeconfig": str(tmp_path / "kubeconfig"),
            "context": "test",
        },
        "nodes": [
            {
                "name": "node-1",
                "uid": "uid-1",
                "ssh_host": "192.0.2.1",
                "ssh_user": "ubuntu",
                "private_key_file": str(tmp_path / "ssh"),
                "known_hosts_file": str(tmp_path / "known_hosts"),
            }
        ],
        "endpoint": {"url": "https://192.0.2.1:8200"},
        "target": {
            "installation_id": "test",
            "node_name": "node-1",
            "bind_address": "192.0.2.1",
        },
        "handoff": {
            "action": "prepare",
            "directory": str(directory),
            "receipt_path": str(directory / "receipt.json"),
            "owner_id": "user-1",
            "ttl_seconds": 60,
        },
    }
