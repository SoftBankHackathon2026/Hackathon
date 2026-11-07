"""Canonical signed documents: RFC 8785 subset (integer timestamps, no floats).

Object keys sort by UTF-16 code units. Invalid Unicode and numbers outside the
exact JavaScript integer range are rejected, not silently coerced.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re

IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{16,80}$")


def canonical(value) -> bytes:
    def encode(item):
        if item is None or isinstance(item, bool):
            return json.dumps(item)
        if isinstance(item, int) and abs(item) <= 9007199254740991:
            return str(item)
        if isinstance(item, str):
            item.encode("utf-8", errors="strict")
            return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if isinstance(item, list):
            return "[" + ",".join(encode(v) for v in item) + "]"
        if isinstance(item, dict) and all(isinstance(k, str) for k in item):
            return (
                "{"
                + ",".join(
                    encode(k) + ":" + encode(item[k])
                    for k in sorted(
                        item, key=lambda k: k.encode("utf-16-be", errors="strict")
                    )
                )
                + "}"
            )
        raise ValueError("unsupported canonical JSON value")

    return encode(value).encode("utf-8")


def signed_bytes(purpose: str, payload: dict) -> bytes:
    return ("migration/" + purpose + "/v1\n").encode("ascii") + canonical(payload)


def digest(value) -> str:
    return "sha384:" + hashlib.sha384(canonical(value)).hexdigest()


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64(value: str, length: int) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid base64url")
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(decoded) != length or b64(decoded) != value:
        raise ValueError("invalid encoded length")
    return decoded


def confirmation_code(payload: dict) -> str:
    # 40 bits, RFC4648 Base32 (no 0/1/8/9), bound to the exact candidate proof.
    return base64.b32encode(hashlib.sha384(canonical(payload)).digest()[:5]).decode()
