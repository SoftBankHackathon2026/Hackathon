"""Server aws.env comes from the Terraform-managed parameter; no keys, atomic, restorable."""

import importlib.util
import os
from pathlib import Path

import pytest

path = Path(__file__).parents[2] / "ops" / "aws_env.py"
spec = importlib.util.spec_from_file_location("aws_env", path)
aws_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aws_env)

SETTINGS = """# managed by Terraform
AWS_REGION=ap-northeast-2
AWS_PROFILE=oneport-deployer
AWS_DEPLOYER_ROLE_ARN=arn:aws:iam::123456789012:role/oneport-deployer
AWS_ECS_CLUSTER_NAME=oneport
ONEPORT_TRACING=xray
"""
OWNER = {"uid": os.getuid(), "gid": os.getgid()}


def test_install_replaces_only_on_change_and_keeps_a_backup(tmp_path):
    target = tmp_path / "aws.env"
    target.write_text("AWS_REGION=old\n")
    assert aws_env.install(SETTINGS, target, **OWNER) is True
    assert target.read_text() == SETTINGS
    assert oct(target.stat().st_mode & 0o777) == "0o640"
    assert (tmp_path / "aws.env.prev").read_text() == "AWS_REGION=old\n"
    assert aws_env.restore(target) is True
    assert target.read_text() == "AWS_REGION=old\n"
    assert aws_env.restore(target) is False


def test_unchanged_settings_leave_nothing_to_restore(tmp_path):
    target = tmp_path / "aws.env"
    target.write_text(SETTINGS)
    (tmp_path / "aws.env.prev").write_text("AWS_REGION=stale\n")
    assert aws_env.install(SETTINGS, target, **OWNER) is False
    assert aws_env.restore(target) is False
    assert target.read_text() == SETTINGS


@pytest.mark.parametrize(
    "text",
    [
        SETTINGS + "AWS_SECRET_ACCESS_KEY=x\n",
        SETTINGS.replace("AWS_PROFILE=oneport-deployer\n", ""),
        SETTINGS + "BAD LINE\n",
        SETTINGS + "ONEPORT_X=two words\n",
    ],
)
def test_invalid_or_credential_settings_are_rejected_before_writing(tmp_path, text):
    target = tmp_path / "aws.env"
    target.write_text("AWS_REGION=old\n")
    with pytest.raises(ValueError):
        aws_env.install(text, target, **OWNER)
    assert target.read_text() == "AWS_REGION=old\n"
    assert list(tmp_path.iterdir()) == [target]
