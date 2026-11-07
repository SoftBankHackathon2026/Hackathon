"""접수 큐(durable-ingress)를 지워도 되는지 — 미처리 요청 0 확인, 읽기만.

202 로 접수한 요청은 Redis Stream 의 기록이 유일한 사본이다. 그래서 Redis 를
지우기 전에(설계 4.18 (l)) 아래가 모두 맞는지 본다.

  * `XINFO GROUPS ingress:inbox` 의 `delivery` 그룹이 pending=0·lag=0.
    ACK 는 커밋을 확인한 뒤에만 하므로(service.js) 접수한 요청이 모두
    커밋됐다는 뜻이다. lag 를 모르면(nil) 맞지 않는 것으로 본다.
  * 접수 큐 /status 가 in_flight=0·last_error 없음.

Redis 는 명령 하나만 쓰므로 의존성을 늘리지 않고 RESP2 로 직접 묻는다.

  problems = ingress_drain.drain_problems(redis_url, ingress)
"""

import socket
import urllib.parse
from typing import Any

from crosscloud import ingress_control

_STREAM = "ingress:inbox"
_GROUP = "delivery"
_TIMEOUT_SECONDS = 5.0
_DEFAULT_PORT = 6379


class RedisReplyError(RuntimeError):
    """Redis 가 오류(-ERR …)로 답했다."""


def drain_problems(
    redis_url: str, ingress: ingress_control.IngressControl
) -> tuple[str, ...]:
    """지우면 안 되는 이유 목록. 빈 tuple 이면 지워도 된다.

    Raises:
        ValueError: redis_url 이 redis:// 주소가 아닐 때.
    """
    address = _address(redis_url)
    problems = []
    try:
        problems.extend(_group_problems(_xinfo_groups(address)))
    except (OSError, RedisReplyError, ValueError) as error:
        problems.append(f"Redis XINFO 실패: {error}")
    try:
        state = ingress.status()
    except ingress_control.IngressError as error:
        problems.append(f"접수 큐 상태를 못 읽음: {error}")
    else:
        if state.in_flight:
            problems.append(f"in_flight={state.in_flight}")
        if state.last_error is not None:
            problems.append(f"last_error={state.last_error}")
    return tuple(problems)


def _group_problems(groups: list[dict[str, Any]]) -> list[str]:
    group = next((g for g in groups if g.get("name") == _GROUP), None)
    if group is None:
        return [f"{_STREAM} 에 {_GROUP} 그룹이 없다"]
    problems = []
    if group.get("pending") != 0:
        problems.append(f"pending={group.get('pending')}")
    if group.get("lag") != 0:
        problems.append(f"lag={group.get('lag')}")
    return problems


def _address(redis_url: str) -> tuple[str, int]:
    parts = urllib.parse.urlsplit(redis_url)
    if parts.scheme != "redis" or not parts.hostname:
        raise ValueError("redis_url 은 redis://<host>:<port> 여야 한다")
    return parts.hostname, parts.port or _DEFAULT_PORT


def _xinfo_groups(address: tuple[str, int]) -> list[dict[str, Any]]:
    command = (b"XINFO", b"GROUPS", _STREAM.encode())
    request = f"*{len(command)}\r\n".encode() + b"".join(
        f"${len(part)}\r\n".encode() + part + b"\r\n" for part in command
    )
    with socket.create_connection(address, timeout=_TIMEOUT_SECONDS) as conn:
        conn.sendall(request)
        reply = _read_reply(conn.makefile("rb"))
    if not isinstance(reply, list):
        raise ValueError("XINFO GROUPS 응답이 배열이 아니다")
    groups = []
    for flat in reply:
        if not isinstance(flat, list) or len(flat) % 2:
            raise ValueError("XINFO GROUPS 항목 형식이 다르다")
        groups.append(dict(zip(flat[0::2], flat[1::2], strict=True)))
    return groups


def _read_reply(stream: Any) -> Any:
    """RESP2 응답 하나를 읽는다. bulk 문자열은 str 로 바꾼다."""
    line = stream.readline()
    if not line.endswith(b"\r\n"):
        raise ValueError("Redis 응답이 끊겼다")
    kind, body = line[:1], line[1:-2].decode("utf-8", "replace")
    if kind == b"+":
        return body
    if kind == b"-":
        raise RedisReplyError(body)
    if kind == b":":
        return int(body)
    if kind == b"$":
        length = int(body)
        if length < 0:
            return None
        return stream.read(length + 2)[:-2].decode("utf-8", "replace")
    if kind == b"*":
        count = int(body)
        return (
            None if count < 0 else [_read_reply(stream) for _ in range(count)]
        )
    raise ValueError(f"모르는 RESP 형식: {kind!r}")
