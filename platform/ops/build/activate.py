"""Run via the fixed CD SSM document, after build infrastructure is provisioned."""

import json
import subprocess
from pathlib import Path

import boto3

from controlplane.builds.models import BuildSettings
from controlplane.demo_deployments.models import RunSettings


def main():
    root = Path("/opt/oneport/current/platform")
    settings = BuildSettings.model_validate_json(
        (root / "deployment/build.json").read_text()
    )
    client = boto3.client("codebuild", region_name=settings.region)
    projects = client.batch_get_projects(names=[settings.project])["projects"]
    if len(projects) != 1:
        raise ValueError("CodeBuild project missing")
    subprocess.run(
        [
            "install",
            "-o",
            "root",
            "-g",
            "oneport",
            "-m",
            "0640",
            str(root / "deployment/build.json"),
            "/etc/oneport/build.json",
        ],
        check=True,
    )
    dropin = Path("/etc/systemd/system/oneport-api.service.d")
    dropin.mkdir(mode=0o755, parents=True, exist_ok=True)
    (dropin / "build.conf").write_text(
        "[Service]\nEnvironment=ONEPORT_BUILD_CONFIG=/etc/oneport/build.json\n"
    )
    subprocess.run(
        [
            "install",
            "-m",
            "0644",
            str(root / "ops/systemd/oneport-build-worker.service"),
            "/etc/systemd/system/oneport-build-worker.service",
        ],
        check=True,
    )
    # Multi-environment deploys build once and reuse these workers: enabled with builds.
    run_settings = RunSettings.model_validate_json(
        (root / "deployment/demo-deploy.json").read_text()
    )
    subprocess.run(
        [
            "install",
            "-o",
            "root",
            "-g",
            "oneport",
            "-m",
            "0640",
            str(root / "deployment/demo-deploy.json"),
            "/etc/oneport/demo-deploy.json",
        ],
        check=True,
    )
    (dropin / "demo-deploy.conf").write_text(
        "[Service]\nEnvironment=ONEPORT_DEMO_DEPLOY_CONFIG=/etc/oneport/demo-deploy.json\n"
    )
    subprocess.run(
        [
            "install",
            "-m",
            "0644",
            str(root / "ops/systemd/oneport-demo-worker.service"),
            "/etc/systemd/system/oneport-demo-worker.service",
        ],
        check=True,
    )
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "restart", "oneport-api"], check=True)
    subprocess.run(["systemctl", "enable", "oneport-build-worker"], check=True)
    subprocess.run(["systemctl", "restart", "oneport-build-worker"], check=True)
    subprocess.run(["systemctl", "enable", "oneport-demo-worker"], check=True)
    # Restart, not just start: a new release must replace a worker left running.
    subprocess.run(["systemctl", "restart", "oneport-demo-worker"], check=True)
    print(
        json.dumps(
            {
                "builds": "enabled",
                "project": settings.project,
                "max_daily": settings.max_daily,
                "max_pending": settings.max_pending,
                "max_deployments": run_settings.max_pending,
            }
        )
    )


if __name__ == "__main__":
    main()
