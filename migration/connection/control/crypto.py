"""Narrow OpenSSL 3.5 bridge; only public keys enter verification commands."""

import subprocess
import tempfile
from pathlib import Path

from migration_connection.contracts.protocol import b64, unb64

# SubjectPublicKeyInfo: id-ml-dsa-65 2.16.840.1.101.3.4.3.18.
MLDSA65_SPKI_PREFIX = bytes.fromhex("308207b2300b0609608648016503040312038207a100")


class OpenSSLCrypto:
    def __init__(self, binary: str, signing_key: str):
        self.binary = binary
        self.signing_key = signing_key

    def verify(self, public_key: str, signature: str, message: bytes) -> bool:
        try:
            raw_key, raw_signature = unb64(public_key, 1952), unb64(signature, 3309)
            with tempfile.TemporaryDirectory(prefix="connection-verify-") as directory:
                root = Path(directory)
                (root / "key.der").write_bytes(MLDSA65_SPKI_PREFIX + raw_key)
                (root / "signature").write_bytes(raw_signature)
                (root / "message").write_bytes(message)
                result = subprocess.run(
                    [
                        self.binary,
                        "pkeyutl",
                        "-verify",
                        "-rawin",
                        "-pubin",
                        "-keyform",
                        "DER",
                        "-inkey",
                        str(root / "key.der"),
                        "-sigfile",
                        str(root / "signature"),
                        "-in",
                        str(root / "message"),
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                return result.returncode == 0
        except (ValueError, OSError, subprocess.SubprocessError):
            return False

    def sign(self, message: bytes) -> str:
        with tempfile.TemporaryDirectory(prefix="connection-sign-") as directory:
            root = Path(directory)
            (root / "message").write_bytes(message)
            result = subprocess.run(
                [
                    self.binary,
                    "pkeyutl",
                    "-sign",
                    "-rawin",
                    "-inkey",
                    self.signing_key,
                    "-in",
                    str(root / "message"),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=5,
                check=False,
            )
            if result.returncode or len(result.stdout) != 3309:
                raise RuntimeError("JOB_SIGNING_UNAVAILABLE")
            return b64(result.stdout)

    def ready(self) -> bool:
        try:
            key = Path(self.signing_key)
            if not key.is_file() or key.stat().st_mode & 0o077:
                return False
            result = subprocess.run(
                [self.binary, "version"], capture_output=True, timeout=3, check=False
            )
            if result.returncode or not result.stdout.startswith(b"OpenSSL 3.5."):
                return False
            public = subprocess.run(
                [
                    self.binary,
                    "pkey",
                    "-in",
                    self.signing_key,
                    "-pubout",
                    "-outform",
                    "DER",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=3,
                check=False,
            )
            return (
                public.returncode == 0
                and len(public.stdout) == len(MLDSA65_SPKI_PREFIX) + 1952
                and public.stdout.startswith(MLDSA65_SPKI_PREFIX)
            )
        except (OSError, subprocess.SubprocessError):
            return False
