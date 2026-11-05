from typing import Any

import httpx

Row = dict[str, Any]
TIMEOUT = httpx.Timeout(3.0, connect=2.0)

# 주소 목록에서 마지막으로 응답한 주소. 수집기는 tick 마다 클라이언트를 새로 만들므로 모듈 단위로 기억한다.
_PREFERRED: dict[str, str] = {}


class FailoverGet:
    """쉼표로 나눈 여러 주소를 차례로 시도한다.

    온프렘 NodePort 는 externalTrafficPolicy: Local 이라 파드가 있는 노드만 응답한다. 파드가 노드를 옮겨도
    수집이 이어지도록 노드 주소를 모두 적고, 연결 단계에서 실패한 주소만 건너뛴다. HTTP 응답 자체(4xx·5xx)는
    다음 주소로 넘기지 않는다.
    """

    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self._key = base_url
        self._bases = [part.strip().rstrip("/") for part in base_url.split(",") if part.strip()]
        if not self._bases:
            raise ValueError("base_url is empty")
        self._client = client or httpx.Client(timeout=TIMEOUT)

    def _ordered(self) -> list[str]:
        preferred = _PREFERRED.get(self._key)
        if preferred not in self._bases:
            return self._bases
        return [preferred, *(base for base in self._bases if base != preferred)]

    def get(self, path: str) -> httpx.Response:
        error: httpx.TransportError | None = None
        for base in self._ordered():
            try:
                response = self._client.get(f"{base}{path}")
            except httpx.TransportError as exc:
                error = exc
                continue
            _PREFERRED[self._key] = base
            return response
        assert error is not None
        raise error


class WorkerStatusClient:
    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self._http = FailoverGet(base_url, client)

    def get_status(self) -> Row:
        response = self._http.get("/status")
        response.raise_for_status()
        return response.json()


class ConnectStatusClient:
    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self._http = FailoverGet(base_url, client)

    def get_status(self, connector: str) -> Row | None:
        response = self._http.get(f"/connectors/{connector}/status")
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        response.raise_for_status()
        return response.json()
