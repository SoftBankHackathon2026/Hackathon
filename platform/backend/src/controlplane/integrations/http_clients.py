from typing import Any

import httpx

Row = dict[str, Any]
TIMEOUT = httpx.Timeout(3.0, connect=2.0)


class WorkerStatusClient:
    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=TIMEOUT)

    def get_status(self) -> Row:
        response = self._client.get(f"{self._base_url}/status")
        response.raise_for_status()
        return response.json()


class ConnectStatusClient:
    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=TIMEOUT)

    def get_status(self, connector: str) -> Row | None:
        response = self._client.get(f"{self._base_url}/connectors/{connector}/status")
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        response.raise_for_status()
        return response.json()
