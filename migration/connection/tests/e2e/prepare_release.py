"""Prepare the signed ARM64-only release for the isolated live test network."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import time
from pathlib import Path


def module(name: str):
    path = Path(__file__).resolve().parents[2] / "releases" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("release tool unavailable")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def prepare(root: Path, openssl: str, sequence: int) -> None:
    os.umask(0o077)
    meta = root.lstat()
    if (
        not stat.S_ISDIR(meta.st_mode)
        or meta.st_uid != os.getuid()
        or stat.S_IMODE(meta.st_mode) != 0o700
    ):
        raise ValueError("current-user-owned 0700 test directory required")
    marker = root / "bin/.build-incomplete"
    if marker.exists() or marker.is_symlink():
        raise ValueError(
            "native build is incomplete; refusing to publish existing binaries"
        )
    if type(sequence) is not int or not 1 <= sequence <= 9_007_199_254_740_991:
        raise ValueError("explicit positive safe-integer release sequence required")
    previous_metadata = root / "public/release-metadata.json"
    if previous_metadata.is_file():
        previous = json.loads(previous_metadata.read_text()).get("release_sequence", 1)
        if type(previous) is not int or sequence <= previous:
            raise ValueError(
                "release sequence must increase beyond the previous publication"
            )
    binary = root / "bin/migration-connector"
    if binary.is_symlink() or not binary.is_file():
        raise ValueError("completed regular native binary required")
    version = "live-e2e-1"
    base = root / "release" / "releases" / version
    bundle = base / "linux-arm64"
    bundle.mkdir(mode=0o700, parents=True, exist_ok=False)
    shutil.copyfile(binary, bundle / "migration-connector")
    (bundle / "migration-connector").chmod(0o700)
    shutil.copyfile(root / "broker-ca.crt", bundle / "root-ca.pem")
    config = {
        "public_api_url": "https://control.test/api/v1/connections",
        "broker_host": "broker.test",
        "broker_port": 8443,
        "root_ca": "root-ca.pem",
        "job_public_key": (root / "public/job-public-key").read_text().strip(),
        "collector_archive": "inventory.pyz",
        "helper_path": "/usr/local/libexec/migration-net-helper",
    }
    (bundle / "connector.json").write_text(
        json.dumps(config, sort_keys=True, separators=(",", ":"))
    )
    module("build_inventory").build(bundle / "inventory.pyz")
    manifest = module("sign_manifest").sign(
        bundle,
        sequence,
        int(time.time() * 1000) + 6 * 3600 * 1000,
        root / "signing/release-mldsa.pem",
        root / "signing/release-ed25519.pem",
        openssl,
    )
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    )
    digest = hashlib.sha384(binary.read_bytes()).hexdigest()
    bootstrap = module("render_bootstrap").render(
        f"https://control.test/releases/{version}", digest, digest
    )
    # Only ARM64 was built for this disposable test. Both template hashes are
    # the real binary digest, but x86_64 is explicitly rejected before download.
    branch = "x86_64) _migration_arch=linux-amd64; _migration_hash=$_migration_amd64 ;;"
    if bootstrap.count(branch) != 1:
        raise ValueError("bootstrap architecture branch changed; review required")
    bootstrap = bootstrap.replace(
        branch, "x86_64) _migration_fail '이 격리 시험 배포는 arm64만 지원합니다.' ;;"
    )
    (base / "bootstrap.sh").write_text(bootstrap)
    metadata = {
        "release_version": version,
        "release_sequence": sequence,
        "bootstrap_url": f"https://control.test/releases/{version}/bootstrap.sh",
        "bootstrap_sha384": hashlib.sha384(bootstrap.encode()).hexdigest(),
        "binary_sha384": digest,
        "architecture": "arm64",
        "bundle_path": str(bundle),
    }
    (root / "public/release-metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n"
    )
    # Only this credential-free subtree is mounted into the HTTPS publisher.
    # The containing test root stays 0700 and private keys remain elsewhere.
    published = root / "release"
    published.chmod(0o755)
    for path in published.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o644)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--openssl", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    args = parser.parse_args()
    prepare(args.directory, args.openssl, args.sequence)
    print("Signed test release prepared; metadata is in public/release-metadata.json.")
