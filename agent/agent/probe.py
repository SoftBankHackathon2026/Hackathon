"""수정 값으로 직접 DB 에 붙어 본다: TCP → TLS·로그인 → SELECT 1. 비밀번호는 환경변수의 접속 URL 에서만 읽는다."""

from __future__ import annotations

import os
import socket
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

import psycopg

from .config import ProbeSettings


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    step: str  # tcp | login | query | done
    error: str | None = None  # 예외 종류만 남긴다. 문구에 접속 정보가 섞일 수 있다


class Prober(Protocol):
    def probe(self, config: Mapping[str, Any]) -> ProbeResult: ...


def password_from_url(url: str | None) -> str | None:
    if not url:
        return None
    raw = urllib.parse.urlsplit(url).password
    return urllib.parse.unquote(raw) if raw is not None else None


class PostgresProber:
    def __init__(
        self, settings: ProbeSettings, *, env: Mapping[str, str] | None = None, sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self._env = os.environ if env is None else env
        self._sleep = sleep

    def _address(self, host: str, port: int) -> tuple[str, int] | None:
        mapped = self.settings.address_map.get(f"{host}:{port}")
        if not mapped:
            return None
        addr, _, mapped_port = str(mapped).rpartition(":")
        if not addr:
            raise ValueError(mapped)
        return addr, int(mapped_port)

    def probe(self, config: Mapping[str, Any]) -> ProbeResult:
        result = self._probe_once(config)
        for _ in range(max(self.settings.attempts, 1) - 1):
            if result.ok or result.error == "AddressMapInvalid":
                break
            self._sleep(self.settings.retry_delay)
            result = self._probe_once(config)
        return result

    def _probe_once(self, config: Mapping[str, Any]) -> ProbeResult:
        host, port = str(config["DB_HOST"]), int(config["DB_PORT"])
        try:
            mapped = self._address(host, port)
        except ValueError:
            return ProbeResult(False, "tcp", "AddressMapInvalid")
        connect_to = mapped or (host, port)
        try:
            socket.create_connection(connect_to, timeout=self.settings.connect_timeout).close()
        except OSError as exc:
            return ProbeResult(False, "tcp", type(exc).__name__)
        kwargs: dict[str, Any] = {
            "host": host, "port": connect_to[1], "dbname": config.get("DB_NAME"), "user": config.get("DB_USER"),
            "password": password_from_url(self._env.get(self.settings.password_url_env)),
            "sslmode": self.settings.sslmode, "connect_timeout": max(int(self.settings.connect_timeout), 1),
        }
        if mapped:
            kwargs["hostaddr"] = mapped[0]
        if self.settings.sslrootcert:
            kwargs["sslrootcert"] = self.settings.sslrootcert
        try:
            conn = psycopg.connect(**kwargs)
        except psycopg.Error as exc:
            return ProbeResult(False, "login", type(exc).__name__)
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
        except psycopg.Error as exc:
            return ProbeResult(False, "query", type(exc).__name__)
        if row != (1,):
            return ProbeResult(False, "query", "UnexpectedResult")
        return ProbeResult(True, "done")
