"""Build the real Ubuntu native programs; only public release anchors enter Docker."""

import argparse
import os
import subprocess
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--image")
    parser.add_argument("--arch", choices=("arm64", "amd64"), default="arm64")
    args = parser.parse_args()
    args.image = args.image or (
        "mc-live-e2e-native:local"
        if args.arch == "arm64"
        else "mc-live-e2e-native:amd64"
    )
    root = args.private_dir.resolve()
    if root.stat().st_mode & 0o077:
        raise SystemExit("private directory must be 0700")
    output = root / "bin" if args.arch == "arm64" else root / "bin/linux-amd64"
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    incomplete = output / ".build-incomplete"
    incomplete.write_text(
        "Latest requested source build has not completed. Do not publish these binaries.\n"
    )
    incomplete.chmod(0o600)
    context = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    required = {
        "MIGRATION_RELEASE_MLDSA_PUBLIC_KEY",
        "MIGRATION_RELEASE_ED25519_PUBLIC_KEY",
        "MIGRATION_MIN_RELEASE_SEQUENCE",
    }
    for line in (root / "public/release-build.env").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        if key not in required or not value or any(c.isspace() for c in value):
            raise SystemExit("invalid public build environment")
        env[key] = value.strip("'\"")
    if not required.issubset(env):
        raise SystemExit("release public anchors missing")
    command = [
        "docker",
        "build",
        "--platform",
        f"linux/{args.arch}",
        "-f",
        "tests/e2e/Dockerfile.native",
        "-t",
        args.image,
    ]
    for key in sorted(required):
        command += ["--build-arg", key]
    command += ["."]
    with (root / f"native-build-{args.arch}.log").open("wb") as log:
        subprocess.run(
            command,
            cwd=context,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    name = "mc-live-e2e-export-" + uuid.uuid4().hex[:10]
    subprocess.run(
        [
            "docker",
            "create",
            "--platform",
            f"linux/{args.arch}",
            "--name",
            name,
            "--label",
            "migration.connection.live-e2e=true",
            args.image,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    try:
        for binary in (
            "migration-connector",
            "migration-key-broker",
            "migration-net-helper",
        ):
            subprocess.run(
                [
                    "docker",
                    "cp",
                    f"{name}:/usr/local/libexec/{binary}",
                    str(output / binary),
                ],
                check=True,
            )
    finally:
        subprocess.run(["docker", "rm", name], check=True, stdout=subprocess.DEVNULL)
    incomplete.unlink()
    print("Ubuntu native image and binaries are ready")


if __name__ == "__main__":
    main()
