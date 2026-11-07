"""대상 DB 탐침. 설정 저장소의 값으로 실제 연결·조회를 해 보고 정책 입력용 check 를 만든다.

이 탐침은 ai-agent 파드에서 수행하는 것이다. 대상 앱(sample-app) 자체의 검사 endpoint 는 7번·18번 태스크에서
연결한다. 그 전까지 APP-01(target_app_db) 증거는 이 탐침 결과다 — 보고에 source 로 구분해 남긴다.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from migration.policy_client import build_check

from .config import ScopeConfig

log = logging.getLogger("agent.probe")

SOURCE = "agent.probe"


@dataclass(frozen=True)
class ProbeResult:
    """연결·조회 결과. error 에는 비밀값이 들어가지 않도록 메시지를 자른다."""

    host: str
    port: int
    tcp_ok: bool
    db_connected: bool
    order_query_ok: bool
    error: str | None = None
    status: str = "pass"  # pass: 탐침 수행됨 / error: 탐침 자체 실패(설정 누락 등)

    def as_check(self, *, app_id: str, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "db_connected": self.db_connected,
            "order_query_ok": self.order_query_ok,
            "tcp_ok": self.tcp_ok,
            "host": self.host,
            "port": self.port,
            "app_id": app_id,
        }
        if self.error:
            detail["error"] = self.error
        if extra:
            detail.update(extra)
        return build_check(self.status, detail, source=SOURCE)


class Prober(Protocol):
    def probe(self, values: Mapping[str, str]) -> ProbeResult: ...


def _sanitize(msg: str, secrets: tuple[str, ...]) -> str:
    out = msg.replace("\n", " ")
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out[:300]


class PostgresProber:
    """TCP 연결 → psycopg 로그인 → `SELECT 1 FROM <table> LIMIT 1`."""

    def __init__(self, scope: ScopeConfig, *, timeout: float = 5.0) -> None:
        self.scope = scope
        self.timeout = timeout

    def probe(self, values: Mapping[str, str]) -> ProbeResult:
        sc = self.scope
        host = values.get(sc.host_key, "")
        port_raw = values.get(sc.port_key, "5432") or "5432"
        try:
            port = int(port_raw)
        except ValueError:
            return ProbeResult(host, 0, False, False, False, f"{sc.port_key} 가 숫자가 아니다: {port_raw!r}", status="error")
        if not host:
            return ProbeResult("", port, False, False, False, f"{sc.host_key} 가 비어 있다", status="error")
        password = values.get(sc.password_key, "")
        secrets = (password,)

        try:
            with socket.create_connection((host, port), timeout=self.timeout):
                pass
        except OSError as exc:
            return ProbeResult(host, port, False, False, False, _sanitize(f"tcp: {exc}", secrets))

        try:
            import psycopg
        except ImportError:  # pragma: no cover
            return ProbeResult(host, port, True, False, False, "psycopg 미설치", status="error")

        conninfo = {
            "host": host, "port": port,
            "dbname": values.get(sc.dbname_key, "postgres") or "postgres",
            "user": values.get(sc.user_key, "postgres") or "postgres",
            "password": password,
            "sslmode": values.get(sc.sslmode_key, "prefer") or "prefer",
            "connect_timeout": max(1, int(self.timeout)),
        }
        try:
            with psycopg.connect(**conninfo) as conn:
                conn.execute("SELECT 1")
                try:
                    with conn.cursor() as cur:
                        cur.execute(psycopg.sql.SQL("SELECT 1 FROM {} LIMIT 1").format(psycopg.sql.Identifier(sc.table)))
                        cur.fetchall()
                    return ProbeResult(host, port, True, True, True)
                except psycopg.Error as exc:
                    return ProbeResult(host, port, True, True, False, _sanitize(f"query: {exc}", secrets))
        except psycopg.Error as exc:
            return ProbeResult(host, port, True, False, False, _sanitize(f"connect: {exc}", secrets))
