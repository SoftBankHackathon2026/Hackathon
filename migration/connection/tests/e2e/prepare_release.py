"""Prepare signed architecture-specific releases for the isolated live test network."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
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


def prepare(
    root: Path,
    openssl: str,
    sequence: int,
    *,
    with_amd64: bool = False,
    version: str = "live-e2e-1",
    client_net_policy: Path | None = None,
) -> None:
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
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", version):
        raise ValueError("invalid release version")
    architectures = ("arm64", "amd64") if with_amd64 else ("arm64",)
    binaries = {}
    for arch in architectures:
        directory = root / "bin" if arch == "arm64" else root / "bin/linux-amd64"
        marker = directory / ".build-incomplete"
        if marker.exists() or marker.is_symlink():
            raise ValueError(
                "native build is incomplete; refusing to publish existing binaries"
            )
        binary = directory / "migration-connector"
        if binary.is_symlink() or not binary.is_file():
            raise ValueError("completed regular native binary required")
        header = binary.read_bytes()[:20]
        machine = 183 if arch == "arm64" else 62
        if (
            len(header) != 20
            or header[:6] != b"\x7fELF\x02\x01"
            or int.from_bytes(header[18:20], "little") != machine
        ):
            raise ValueError("binary architecture does not match " + arch)
        binaries[arch] = binary
    base = root / "release" / "releases" / version
    base.mkdir(mode=0o700, parents=True, exist_ok=False)
    digests = {}
    for arch, binary in binaries.items():
        bundle = base / ("linux-" + arch)
        bundle.mkdir(mode=0o700)
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
        if client_net_policy is not None:
            helper = binary.parent / "migration-net-helper"
            if helper.is_symlink() or not helper.is_file():
                raise ValueError("completed regular network helper required")
            policy = json.loads(client_net_policy.read_text())
            if policy.get("role") != "client" or policy.get("uid") != 0:
                raise ValueError(
                    "signed client policy must use uid=0 installation placeholder"
                )
            shutil.copyfile(helper, bundle / "migration-net-helper")
            (bundle / "net-policy.json").write_text(
                json.dumps(policy, sort_keys=True, separators=(",", ":"))
            )
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
        digests[arch] = hashlib.sha384(binary.read_bytes()).hexdigest()
    bootstrap = module("render_bootstrap").render(
        f"https://control.test/releases/{version}",
        digests.get("amd64", digests["arm64"]),
        digests["arm64"],
    )
    if not with_amd64:
        branch = (
            "x86_64) _migration_arch=linux-amd64; _migration_hash=$_migration_amd64 ;;"
        )
        if bootstrap.count(branch) != 1:
            raise ValueError("bootstrap architecture branch changed; review required")
        bootstrap = bootstrap.replace(
            branch,
            "x86_64) _migration_fail '이 격리 시험 배포는 arm64만 지원합니다.' ;;",
        )
    (base / "bootstrap.sh").write_text(bootstrap)
    metadata = {
        "release_version": version,
        "release_sequence": sequence,
        "bootstrap_url": f"https://control.test/releases/{version}/bootstrap.sh",
        "bootstrap_sha384": hashlib.sha384(bootstrap.encode()).hexdigest(),
        "binary_sha384": digests["arm64"],
        "architecture": "multi" if with_amd64 else "arm64",
        "binary_sha384_by_arch": digests,
        "bundle_path": str(base / "linux-arm64"),
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
    parser.add_argument("--with-amd64", action="store_true")
    parser.add_argument("--version", default="live-e2e-1")
    parser.add_argument("--client-net-policy", type=Path)
    args = parser.parse_args()
    prepare(
        args.directory,
        args.openssl,
        args.sequence,
        with_amd64=args.with_amd64,
        version=args.version,
        client_net_policy=args.client_net_policy,
    )
    print("Signed test release prepared; metadata is in public/release-metadata.json.")
