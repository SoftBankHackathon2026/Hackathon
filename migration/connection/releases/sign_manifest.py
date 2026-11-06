"""Offline publisher command. Signing private keys never enter release output."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import tempfile
import time
from pathlib import Path

ARTIFACTS = ("migration-connector", "connector.json", "root-ca.pem", "inventory.pyz")


def sign(
    bundle: Path,
    sequence: int,
    expires_at_ms: int,
    mldsa_key: Path,
    ed25519_key: Path,
    openssl: str = "openssl",
) -> dict:
    if (
        not 1 <= sequence <= 9_007_199_254_740_991
        or not int(time.time() * 1000) < expires_at_ms <= 9_007_199_254_740_991
    ):
        raise ValueError("valid sequence and future expiry required")
    artifacts = []
    total = 0
    for name in ARTIFACTS:
        path = bundle / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("required regular release artifact missing")
        size = path.stat().st_size
        total += size
        if not 0 < size <= 256 * 1024 * 1024 or total > 512 * 1024 * 1024:
            raise ValueError("release size limit exceeded")
        artifacts.append(
            {
                "name": name,
                "sha384": hashlib.sha384(path.read_bytes()).hexdigest(),
                "size": size,
            }
        )
    payload = {
        "version": 1,
        "sequence": sequence,
        "expires_at_ms": expires_at_ms,
        "artifacts": artifacts,
    }
    message = (
        b"migration/release/v1\n"
        + json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )
    signatures = []
    with tempfile.TemporaryDirectory(prefix="migration-release-sign-") as directory:
        source = Path(directory) / "message"
        source.write_bytes(message)
        for key, length in ((mldsa_key, 3309), (ed25519_key, 64)):
            result = subprocess.run(
                [
                    openssl,
                    "pkeyutl",
                    "-sign",
                    "-rawin",
                    "-inkey",
                    str(key),
                    "-in",
                    str(source),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            if result.returncode != 0 or len(result.stdout) != length:
                raise ValueError("release signing failed or wrong signing algorithm")
            signatures.append(
                base64.urlsafe_b64encode(result.stdout).decode().rstrip("=")
            )
    return {
        "payload": payload,
        "mldsa_signature": signatures[0],
        "ed25519_signature": signatures[1],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--sequence", required=True, type=int)
    parser.add_argument("--expires-at-ms", required=True, type=int)
    parser.add_argument("--mldsa-key", required=True, type=Path)
    parser.add_argument("--ed25519-key", required=True, type=Path)
    parser.add_argument("--openssl", default="openssl")
    args = parser.parse_args()
    result = sign(
        args.bundle,
        args.sequence,
        args.expires_at_ms,
        args.mldsa_key,
        args.ed25519_key,
        args.openssl,
    )
    with (args.bundle / "manifest.json").open("x") as output:
        json.dump(result, output, sort_keys=True, separators=(",", ":"))
