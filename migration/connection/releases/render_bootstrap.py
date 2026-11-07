"""Render publisher-controlled URL/hashes; never accepts enrollment secrets."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import urlsplit


def render(base_url: str, amd64: str, arm64: str) -> str:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "HTTPS publisher URL without credentials/query/fragment required"
        )
    if not re.fullmatch(r"https://[A-Za-z0-9._:/-]+", base_url) or any(
        x in (".", "..") for x in parsed.path.split("/")
    ):
        raise ValueError("publisher URL contains unsupported characters")
    if any(not re.fullmatch(r"[0-9a-f]{96}", value) for value in (amd64, arm64)):
        raise ValueError("both architecture SHA-384 hashes are required")
    template = (
        Path(__file__).resolve().parents[1] / "bootstrap/install.sh.in"
    ).read_text()
    return (
        template.replace("@RELEASE_BASE_URL@", base_url.rstrip("/"))
        .replace("@AMD64_SHA384@", amd64)
        .replace("@ARM64_SHA384@", arm64)
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--amd64-sha384", required=True)
    parser.add_argument("--arm64-sha384", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with args.output.open("x") as output:
        output.write(render(args.base_url, args.amd64_sha384, args.arm64_sha384))
