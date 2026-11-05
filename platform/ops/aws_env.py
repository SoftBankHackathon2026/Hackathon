"""Render /etc/oneport/aws.env from the Terraform-managed SSM parameter.

The parameter holds only non-secret AWS settings (IDs, ARNs, names; infra/aws
platform-server.tf). The server authenticates with its instance role through the
AWS_PROFILE named in the file, so access keys in the parameter are rejected.

Exit codes for deploy-release: 0 unchanged, 3 updated (restart readers), 1 failed.
"""

import argparse
import os
import re
import shutil
import tempfile
from pathlib import Path

import boto3

PARAMETER = "/oneport/platform/aws-env"
TARGET = Path("/etc/oneport/aws.env")
REQUIRED = {
    "AWS_REGION",
    "AWS_PROFILE",
    "AWS_DEPLOYER_ROLE_ARN",
    "AWS_ECS_CLUSTER_NAME",
}
FORBIDDEN = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
LINE = re.compile(r"^[A-Z][A-Z0-9_]*=\S*$")
UPDATED = 3


def validate(text: str) -> None:
    keys = set()
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if not LINE.fullmatch(line):
            raise ValueError("invalid setting line")
        keys.add(line.split("=", 1)[0])
    if keys & FORBIDDEN:
        raise ValueError("credentials are not allowed in the settings parameter")
    if REQUIRED - keys:
        raise ValueError("required settings are missing")


def backup_of(target: Path) -> Path:
    return target.with_name(target.name + ".prev")


def install(text: str, target: Path, *, uid: int, gid: int) -> bool:
    """Replace `target` atomically when the content changed; the old file becomes `.prev`."""
    validate(text)
    text = text.rstrip("\n") + "\n"
    # A backup left by an interrupted run must not be restored over this one.
    backup_of(target).unlink(missing_ok=True)
    if target.exists() and target.read_text() == text:
        return False
    fd, temporary = tempfile.mkstemp(prefix=".aws-env-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchown(stream.fileno(), uid, gid)
            os.fchmod(stream.fileno(), 0o640)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if target.exists():
            shutil.copy2(target, backup_of(target))
            os.chown(backup_of(target), uid, gid)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def restore(target: Path) -> bool:
    """Put back the file `install` replaced (deploy rollback)."""
    if not backup_of(target).exists():
        return False
    os.replace(backup_of(target), target)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "restore", "discard"])
    args = parser.parse_args()
    try:
        if args.command == "restore":
            print("aws.env restored" if restore(TARGET) else "aws.env backup not found")
            return 0
        if args.command == "discard":
            backup_of(TARGET).unlink(missing_ok=True)
            return 0
        import grp

        client = boto3.client("ssm", region_name="ap-northeast-2")
        text = client.get_parameter(Name=PARAMETER)["Parameter"]["Value"]
        changed = install(text, TARGET, uid=0, gid=grp.getgrnam("oneport").gr_gid)
        print("aws.env updated" if changed else "aws.env unchanged")
        return UPDATED if changed else 0
    except Exception as exc:  # noqa: BLE001 -- keep the message short; values are not secret
        print(f"aws.env sync failed: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
