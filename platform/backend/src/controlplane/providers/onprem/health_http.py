import ssl
from pathlib import Path

import httpx

RELEASE_HEADER = "X-Launchpad-Release"


def build_health_client(
    *, cafile: Path | None = None, timeout: httpx.Timeout | float = 5
) -> httpx.Client:
    return httpx.Client(
        verify=ssl.create_default_context(cafile=cafile),
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    )


def serves_release(response: httpx.Response, release_id: str) -> bool:
    return response.status_code == 200 and response.headers.get(RELEASE_HEADER) == release_id
