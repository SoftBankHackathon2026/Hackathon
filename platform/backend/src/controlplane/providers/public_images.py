"""Anonymous Docker Hub metadata verification shared by all deployment workers.

Only canonical, digest-pinned Docker Hub references are fetched. Registry auth
and CDN hosts are fixed here, never supplied by the deployment request. No Docker
daemon or push credentials are needed on the API/worker host.
"""

import hashlib
import json
import logging
import re
from urllib.parse import urlsplit

import httpx

from controlplane.providers.contracts import ProviderRejected, ProviderUnavailable

REFERENCE = re.compile(
    r"docker\.io/([a-z0-9]+(?:[._-][a-z0-9]+)*/[a-z0-9]+(?:[._-][a-z0-9]+)*)"
    r"@(sha256:[0-9a-f]{64})"
)
ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
ARCHITECTURES = {"X86_64": "amd64", "ARM64": "arm64"}
MAX_METADATA = 4 * 1024 * 1024


class _RegistryLogFilter(logging.Filter):
    """httpx INFO records include full URLs; strip signed CDN query parameters."""

    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(
                arg.copy_with(query=None) if isinstance(arg, httpx.URL) else arg
                for arg in record.args
            )
        return True


logging.getLogger("httpx").addFilter(_RegistryLogFilter())


def parse_public_image(image: str) -> tuple[str, str]:
    match = REFERENCE.fullmatch(image)
    if not match:
        raise ValueError("docker.io/<namespace>/<repository>@sha256:<64 hex> 주소가 필요합니다.")
    return match.group(1), match.group(2)


def _cdn(url: str) -> bool:
    u = urlsplit(url)
    return (
        u.scheme == "https"
        and u.port in (None, 443)
        and not u.username
        and not u.password
        and (
            u.hostname in {"production.cloudflare.docker.com", "production.cloudfront.docker.com"}
            or (u.hostname or "").endswith(".r2.cloudflarestorage.com")
        )
    )


def _read(client, url, *, headers=None, blob=False):
    for attempt in range(3):
        with client.stream("GET", url, headers=headers) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                url = response.headers.get("location", "")
                if not blob or not _cdn(url) or attempt == 2:
                    raise ProviderRejected("image_registry_redirect_rejected")
                headers = None  # Never forward a registry bearer token to a CDN.
                continue
            if response.status_code == 429 or response.status_code >= 500:
                raise ProviderUnavailable("image_registry_temporarily_unavailable")
            if response.status_code != 200:
                raise ProviderRejected("image_not_public_or_missing")
            data = bytearray()
            for part in response.iter_bytes():
                data.extend(part)
                if len(data) > MAX_METADATA:
                    raise ProviderRejected("image_metadata_too_large")
            return bytes(data)
    raise ProviderRejected("image_registry_redirect_rejected")


def _document(client, url, *, digest=None, headers=None, blob=False):
    raw = _read(client, url, headers=headers, blob=blob)
    if digest and "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
        raise ProviderRejected("image_digest_mismatch")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ProviderRejected("image_metadata_invalid")
    return value


def verify_public_image(
    image: str, architecture="X86_64", *, transport=None, include_runtime=False
) -> dict:
    try:
        repository, digest = parse_public_image(image)
        expected = ARCHITECTURES[architecture]
        with httpx.Client(
            timeout=15, follow_redirects=False, trust_env=False, transport=transport
        ) as client:
            token = _document(
                client,
                "https://auth.docker.io/token?service=registry.docker.io"
                f"&scope=repository:{repository}:pull",
            ).get("token")
            if not isinstance(token, str) or not token or len(token) > 32768:
                raise ProviderRejected("image_registry_auth_failed")
            headers = {"Authorization": "Bearer " + token, "Accept": ACCEPT}
            base = f"https://registry-1.docker.io/v2/{repository}"
            manifest = _document(
                client, base + "/manifests/" + digest, digest=digest, headers=headers
            )
            if "manifests" in manifest:
                candidates = [
                    m
                    for m in manifest["manifests"]
                    if m.get("platform", {}).get("os") == "linux"
                    and m.get("platform", {}).get("architecture") == expected
                ]
                if not candidates:
                    raise ProviderRejected("image_architecture_mismatch")
                child = candidates[0]["digest"]
                if not re.fullmatch(r"sha256:[0-9a-f]{64}", child):
                    raise ProviderRejected("image_metadata_invalid")
                manifest = _document(
                    client, base + "/manifests/" + child, digest=child, headers=headers
                )
            config_digest = manifest["config"]["digest"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", config_digest):
                raise ProviderRejected("image_metadata_invalid")
            config = _document(
                client,
                base + "/blobs/" + config_digest,
                digest=config_digest,
                headers=headers,
                blob=True,
            )
            if config.get("os") != "linux" or config.get("architecture") != expected:
                raise ProviderRejected("image_architecture_mismatch")
            result = {"image": image, "platform": "linux/" + expected, "access": "public"}
            if include_runtime:
                from controlplane.domain.instrumentation import detect

                result["instrumentation"] = detect(config.get("config", {}))
            return result
    except httpx.HTTPError:
        raise ProviderUnavailable("image_registry_temporarily_unavailable") from None
    except (KeyError, TypeError, ValueError):
        raise ProviderRejected("image_metadata_invalid") from None
