"""Create disposable test trust in an explicitly selected private directory.

Never prints credentials or overwrites existing material. No production trust
store, cluster account or cluster configuration is modified.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import stat
import subprocess
from pathlib import Path


def write(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
    path.chmod(0o600)


def invoke(openssl: str, *arguments: str) -> bytes:
    result = subprocess.run(
        [openssl, *arguments], capture_output=True, check=False, timeout=60
    )
    if result.returncode:
        raise RuntimeError("test trust command failed; diagnostic output withheld")
    return result.stdout


def tlv(data: bytes, offset: int = 0) -> tuple[int, bytes, int]:
    tag, length = data[offset], data[offset + 1]
    cursor = offset + 2
    if length & 0x80:
        count = length & 0x7F
        if not 1 <= count <= 4:
            raise ValueError("invalid public-key DER length")
        length = int.from_bytes(data[cursor : cursor + count], "big")
        cursor += count
    end = cursor + length
    if end > len(data):
        raise ValueError("truncated public key")
    return tag, data[cursor:end], end


def public_key(openssl: str, private: Path, expected: int) -> str:
    der = invoke(openssl, "pkey", "-in", str(private), "-pubout", "-outform", "DER")
    tag, sequence, end = tlv(der)
    if tag != 0x30 or end != len(der):
        raise ValueError("invalid public-key sequence")
    algorithm, _, offset = tlv(sequence)
    tag, bitstring, end = tlv(sequence, offset)
    if (
        algorithm != 0x30
        or tag != 3
        or end != len(sequence)
        or bitstring[:1] != b"\x00"
        or len(bitstring) != expected + 1
    ):
        raise ValueError("unexpected public-key algorithm or size")
    return base64.urlsafe_b64encode(bitstring[1:]).decode().rstrip("=")


def generate(root: Path, openssl: str) -> None:
    os.umask(0o077)
    metadata = root.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("existing current-user-owned 0700 test directory required")
    for filename in (
        "control.key",
        "control.crt",
        "ca.crt",
        "ca.key",
        "broker.key",
        "broker.crt",
        "broker-ca.key",
        "broker-ca.crt",
        "job-signing-key.pem",
        "runtime-token",
        "signing",
        "public",
    ):
        if (root / filename).exists() or (root / filename).is_symlink():
            raise ValueError("refusing to replace existing test trust")
    (root / "signing").mkdir(mode=0o700)
    (root / "public").mkdir(mode=0o700)

    def key(path: Path, algorithm: str) -> None:
        arguments = ["genpkey", "-algorithm", algorithm]
        if algorithm == "EC":
            arguments.extend(["-pkeyopt", "ec_paramgen_curve:P-256"])
        write(path, invoke(openssl, *arguments))

    def certificate(prefix: str, hostname: str, algorithm: str, ca_name: str) -> None:
        ca_key, ca_cert = root / f"{ca_name}.key", root / f"{ca_name}.crt"
        leaf_key, leaf_cert = root / f"{prefix}.key", root / f"{prefix}.crt"
        key(ca_key, algorithm)
        write(
            ca_cert,
            invoke(
                openssl,
                "req",
                "-new",
                "-x509",
                "-key",
                str(ca_key),
                "-subj",
                f"/CN=Disposable live E2E {ca_name}",
                "-days",
                "1",
                "-addext",
                "basicConstraints=critical,CA:TRUE,pathlen:0",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
            ),
        )
        key(leaf_key, algorithm)
        csr = root / f"{prefix}.csr"
        ext = root / f"{prefix}.ext"
        write(
            csr,
            invoke(
                openssl,
                "req",
                "-new",
                "-key",
                str(leaf_key),
                "-subj",
                f"/CN={hostname}",
            ),
        )
        write(
            ext,
            f"basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:{hostname}\n".encode(),
        )
        write(
            leaf_cert,
            invoke(
                openssl,
                "x509",
                "-req",
                "-in",
                str(csr),
                "-CA",
                str(ca_cert),
                "-CAkey",
                str(ca_key),
                "-set_serial",
                str(secrets.randbits(120) + 1),
                "-days",
                "1",
                "-extfile",
                str(ext),
            ),
        )
        invoke(
            openssl,
            "verify",
            "-CAfile",
            str(ca_cert),
            "-verify_hostname",
            hostname,
            str(leaf_cert),
        )
        csr.unlink()
        ext.unlink()

    certificate("control", "control.test", "EC", "ca")
    certificate("broker", "broker.test", "ML-DSA-65", "broker-ca")
    mldsa, ed25519 = (
        root / "signing/release-mldsa.pem",
        root / "signing/release-ed25519.pem",
    )
    key(mldsa, "ML-DSA-65")
    key(ed25519, "ED25519")
    key(root / "job-signing-key.pem", "ML-DSA-65")
    write(root / "runtime-token", (secrets.token_urlsafe(48) + "\n").encode())
    public = {
        "MIGRATION_RELEASE_MLDSA_PUBLIC_KEY": public_key(openssl, mldsa, 1952),
        "MIGRATION_RELEASE_ED25519_PUBLIC_KEY": public_key(openssl, ed25519, 32),
        "MIGRATION_MIN_RELEASE_SEQUENCE": "1",
    }
    write(
        root / "public/release-build.env",
        "".join(f"{name}={value}\n" for name, value in public.items()).encode(),
    )
    write(
        root / "public/job-public-key",
        (public_key(openssl, root / "job-signing-key.pem", 1952) + "\n").encode(),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--openssl", required=True)
    args = parser.parse_args()
    generate(args.directory, args.openssl)
    print("Disposable test trust prepared; credentials were not printed.")
