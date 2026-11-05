"""Credential values never enter artifacts/SSM arguments; reject invalid bundles before writes."""

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

path = Path(__file__).parents[2] / "ops" / "provider_secrets.py"
spec = importlib.util.spec_from_file_location("provider_secrets", path)
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


@pytest.fixture
def values():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return {
        "ONEPORT_GCP_CREDENTIALS_JSON": json.dumps(
            {
                "type": "service_account",
                "client_email": "test@demo.iam.gserviceaccount.com",
                "private_key": private,
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        ),
        "ONEPORT_ONPREM_KUBECONFIG": yaml.safe_dump(
            {
                "current-context": "oneport-onprem",
                "contexts": [
                    {
                        "name": "oneport-onprem",
                        "context": {"cluster": "oneport", "user": "deployer"},
                    }
                ],
                "clusters": [
                    {
                        "name": "oneport",
                        "cluster": {
                            "server": "https://kube-api.yubin.website",
                            "insecure-skip-tls-verify": False,
                        },
                    }
                ],
                "users": [{"name": "deployer", "user": {"token": "ephemeral-test-token"}}],
            }
        ),
    }


def test_publish_uses_only_two_fixed_names_and_stable_versions(values):
    client = Mock()
    client.describe_secret.return_value = {"VersionIdsToStages": {}}
    transport.publish(client, values)
    first = client.put_secret_value.call_args_list[:]
    transport.publish(client, values)
    assert client.put_secret_value.call_args_list[2:] == first
    assert {c.kwargs["SecretId"] for c in first} == {v[0] for v in transport.SPECS.values()}
    assert all("SecretString" in c.kwargs for c in first)
    client.get_secret_value.assert_not_called()


def test_restoring_previous_bytes_promotes_version(values):
    client = Mock()
    client.describe_secret.return_value = {
        "VersionIdsToStages": {"old-current-version": ["AWSCURRENT"]}
    }
    transport.publish(client, values)
    assert client.update_secret_version_stage.call_count == 2
    assert (
        client.update_secret_version_stage.call_args.kwargs["RemoveFromVersionId"]
        == "old-current-version"
    )


@pytest.mark.parametrize("change", ["empty", "json", "exec", "tls", "endpoint"])
def test_bad_bundle_never_writes_remote_secret(values, change):
    if change == "empty":
        values["ONEPORT_GCP_CREDENTIALS_JSON"] = ""
    elif change == "json":
        values["ONEPORT_GCP_CREDENTIALS_JSON"] = "token-not-json"
    else:
        k = yaml.safe_load(values["ONEPORT_ONPREM_KUBECONFIG"])
        if change == "exec":
            k["users"][0]["user"] = {"exec": {"command": "untrusted-command"}}
        elif change == "tls":
            k["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
        else:
            k["clusters"][0]["cluster"]["server"] = "https://wrong.example"
        values["ONEPORT_ONPREM_KUBECONFIG"] = yaml.safe_dump(k)
    client = Mock()
    with pytest.raises((ValueError, KeyError)):
        transport.publish(client, values)
    client.put_secret_value.assert_not_called()


def test_install_validates_whole_bundle_before_replacing_files(values, tmp_path):
    target = tmp_path / "secrets"
    target.mkdir()
    old = target / "gcp-credentials.json"
    old.write_text("previous")
    client = Mock()
    client.get_secret_value.side_effect = [
        {"SecretString": values["ONEPORT_GCP_CREDENTIALS_JSON"]},
        {"SecretString": "invalid"},
    ]
    with pytest.raises((ValueError, AttributeError)):
        transport.install(client, target, uid=os.getuid(), gid=os.getgid())
    assert old.read_text() == "previous"
    assert len(list(target.iterdir())) == 1


def test_install_limits_file_permissions_and_cleans_staging(values, tmp_path):
    client = Mock()
    client.get_secret_value.side_effect = [{"SecretString": value} for value in values.values()]
    transport.install(client, tmp_path / "secrets", uid=os.getuid(), gid=os.getgid())
    result = tmp_path / "secrets"
    assert (result.stat().st_mode & 0o777) == 0o750
    assert len(list(result.iterdir())) == 2
    for name, (_, filename) in transport.SPECS.items():
        f = result / filename
        assert (f.stat().st_mode & 0o777) == 0o640
        assert f.read_text() == values[name]
