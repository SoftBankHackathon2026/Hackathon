"""OpenBao KV v2 저장소. 값은 로그와 예외에 넣지 않는다. KV v2 쓰기는 데이터 전체를 교체하므로 읽어서 합친 뒤 cas 로 쓴다."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


class KvError(RuntimeError):
    """저장소 호출 실패 (연결 실패, 봉인, 권한). 메시지에 값·토큰을 넣지 않는다."""


class CasConflict(KvError):
    """읽은 버전과 현재 버전이 다르다. 다른 누군가가 그사이 값을 바꿨다."""


@dataclass(frozen=True)
class KvSnapshot:
    data: Mapping[str, Any] = field(repr=False)
    version: int


class KvStore(Protocol):
    def read(self) -> KvSnapshot: ...

    def write_changes(self, changes: Mapping[str, str], *, expected_version: int) -> int: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """standby 리다이렉트를 따라가면 토큰이 다른 주소로 갈 수 있다."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class OpenBaoKv:
    def __init__(self, addr: str, mount: str, path: str, token: str, *, timeout: float = 5.0) -> None:
        self._url = f"{addr.rstrip('/')}/v1/{urllib.parse.quote(mount.strip('/'))}/data/{urllib.parse.quote(path.strip('/'))}"
        self._token = token
        self._timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def __repr__(self) -> str:
        return f"OpenBaoKv({self._url!r})"

    def _call(self, method: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self._url, data=data, method=method, headers={
            "X-Vault-Token": self._token, "Content-Type": "application/json", "Accept": "application/json",
        })
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                text = exc.read(2048).decode("utf-8", "replace")
            if exc.code == 400 and "check-and-set" in text:
                raise CasConflict("cas 충돌: 읽은 버전과 현재 버전이 다르다") from None
            raise KvError(f"OpenBao HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise KvError(f"OpenBao 호출 실패: {type(exc).__name__}") from None

    def read(self) -> KvSnapshot:
        doc = self._call("GET")
        try:
            data, version = doc["data"]["data"], doc["data"]["metadata"]["version"]
        except (KeyError, TypeError):
            raise KvError("OpenBao 응답 형식이 KV v2 가 아니다") from None
        if not isinstance(data, Mapping) or not isinstance(version, int):  # 삭제된 버전이면 data 가 null 이다
            raise KvError("OpenBao 현재 버전에 데이터가 없다 (삭제됨)")
        return KvSnapshot(dict(data), version)

    def write_changes(self, changes: Mapping[str, str], *, expected_version: int) -> int:
        current = self.read()
        if current.version != expected_version:
            raise CasConflict(f"cas 충돌: 읽은 버전 {expected_version}, 현재 버전 {current.version}")
        doc = self._call("POST", {"options": {"cas": expected_version}, "data": {**current.data, **changes}})
        try:
            return int(doc["data"]["version"])
        except (KeyError, TypeError, ValueError):
            raise KvError("OpenBao 쓰기 응답에 version 이 없다") from None
