"""데모 트래픽 제어 온프렘 에이전트. 표준 라이브러리만 쓴다 (migration-tools 이미지에서 실행).

온프렘에서 화면 서버(server.js)와 한 파드에 돌며, 서버에 붙어 사람이 누른 명령을 봇에 전달한다.
  - 2초마다 서명한 보고(POST /agent/report)를 보내고 명령(GET /agent/commands)을 가져간다.
  - 명령은 사람이 누른 것만 한 번씩 실행한다. 상태를 스스로 맞추거나 재시도하지 않는다.
  - 전환이 진행 중이면(커넥터·종료 안 된 작업·워커 SWITCHING) 실행하지 않고 거절을 보고한다.
설계: demo/traffic-control/DESIGN.md
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

VERSION = "1"
TERMINAL = {"COMPLETED", "ABORTED", "ROLLED_BACK"}


# ---- 서명 (lib/auth.js 와 같은 규칙) -----------------------------------------

def auth_header(key: str, role: str, method: str, path: str, body: bytes,
                timestamp: int | None = None, nonce: str | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    n = nonce or secrets.token_hex(16)
    message = "\n".join([role, method.upper(), path, str(ts), n, hashlib.sha256(body).hexdigest()])
    signature = hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
    return f"{role}.{ts}.{n}.{signature}"


# ---- HTTP -------------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, code: str, status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def http_json(method: str, url: str, *, body: Any = None, headers: dict[str, str] | None = None,
              timeout: float = 5.0) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(1024 * 1024)
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read(64 * 1024) or b"{}")
        except ValueError:
            detail = {}
        code = detail.get("error") if isinstance(detail, dict) else None
        raise HttpError(str(code or f"HTTP_{exc.code}"), exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise HttpError(type(exc).__name__) from None


# ---- 설정 -------------------------------------------------------------------

@dataclass
class Settings:
    control_url: str
    agent_key: str
    worker_url: str = "http://db-insert-worker:8080"
    bot_url: str = "http://traffic-bot-control:9091"
    bot_token: str | None = None
    connect_url: str = "http://migration-connect:8083"
    cutover_db: str = "/var/lib/worker/cutover.sqlite3"
    poll_seconds: float = 2.0
    worker_status_seconds: float = 10.0

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "Settings":
        key = env.get("AGENT_KEY", "")
        if len(key) < 32:
            raise SystemExit("AGENT_KEY must be at least 32 characters")
        url = env.get("CONTROL_URL", "").rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise SystemExit("CONTROL_URL must be http(s)")
        return cls(
            control_url=url, agent_key=key,
            worker_url=env.get("WORKER_URL", cls.worker_url).rstrip("/"),
            bot_url=env.get("BOT_CONTROL_URL", cls.bot_url).rstrip("/"),
            bot_token=env.get("BOT_CONTROL_TOKEN") or None,
            connect_url=env.get("CONNECT_URL", cls.connect_url).rstrip("/"),
            cutover_db=env.get("CUTOVER_DB", cls.cutover_db),
            poll_seconds=float(env.get("POLL_SECONDS", "2")),
            worker_status_seconds=float(env.get("WORKER_STATUS_SECONDS", "10")),
        )


# ---- 관측 -------------------------------------------------------------------

def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def summarize_worker(status: dict[str, Any]) -> dict[str, Any]:
    """워커 /status → 상태판 필드. 시연 예산 모드와 미지원 버전은 제어하지 않는다."""
    budget = status.get("max_generated") is not None
    supported = "generation_enabled" in status
    mode = "budget" if budget else ("continuous" if supported else "unsupported")
    generation = None
    if supported and not budget:
        generation = "running" if status.get("generation_enabled") else "stopped"
    store = status.get("store_counts") or {}
    reason = None
    if budget:
        reason = "시연 예산 모드: 생성량은 전환 도구가 정합니다"
    elif not supported:
        reason = "이 워커 버전에는 생성 스위치가 없습니다"
    return {
        "reachable": True, "mode": mode, "generation": generation,
        "controllable": mode == "continuous",
        "state": status.get("state"), "target": status.get("target"),
        "rate_per_second": status.get("rate_per_second"),
        "pending": status.get("queue_length"), "committed": store.get("committed"),
        "generated_total": status.get("generated_total"),
        "generation_limit": status.get("generation_limit"),
        "max_generated": status.get("max_generated"),
        "reason": reason, "observed_at": now_iso(),
    }


def summarize_bot(control: dict[str, Any], token_present: bool) -> dict[str, Any]:
    generation = control.get("generation")
    ok = generation in ("running", "stopped")
    reason = None
    if not ok:
        reason = "봇 응답 형식이 다릅니다"
    elif not token_present:
        reason = "에이전트에 BOT_CONTROL_TOKEN 이 없습니다"
    return {
        "reachable": True, "mode": "continuous" if ok else "unsupported",
        "generation": generation if ok else None,
        "controllable": ok and token_present,
        "rate_per_second": control.get("rate_per_second"),
        "pending": control.get("pending"), "committed": control.get("committed"),
        "failed": control.get("failed"), "reason": reason, "observed_at": now_iso(),
    }


def unreachable(error: str, reason: str | None = None) -> dict[str, Any]:
    return {"reachable": False, "controllable": False, "error": error[:200],
            "reason": reason, "observed_at": now_iso()}


def open_cutover_jobs(db_path: str) -> list[str]:
    """종료되지 않은 전환 작업. manual_resolution 기록이 붙은 작업은 종료로 본다."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        rows = conn.execute("SELECT job_id, stage, checkpoint FROM cutover_jobs").fetchall()
    finally:
        conn.close()
    out = []
    for job_id, stage, checkpoint in rows:
        if stage in TERMINAL:
            continue
        try:
            resolved = "manual_resolution" in json.loads(checkpoint or "{}")
        except ValueError:
            resolved = False
        if not resolved:
            out.append(f"{job_id}={stage}")
    return out


def compute_lock(*, connectors: list[str] | None, connect_error: str | None,
                 open_jobs: list[str] | None, jobs_error: str | None,
                 worker_state: str | None) -> dict[str, Any]:
    """전환 중이면 잠근다. 확인할 수 없는 것도 잠금 사유다(실패하면 닫힘)."""
    reasons = []
    if connect_error:
        reasons.append(f"Kafka Connect 확인 실패({connect_error})")
    elif connectors:
        reasons.append(f"CDC 커넥터 {len(connectors)}개: {', '.join(sorted(connectors))[:120]}")
    if jobs_error:
        reasons.append(f"전환 상태 확인 실패({jobs_error})")
    elif open_jobs:
        reasons.append(f"끝나지 않은 전환 작업: {', '.join(open_jobs)[:120]}")
    if worker_state == "SWITCHING":
        reasons.append("워커가 대상 DB 를 바꾸는 중(SWITCHING)")
    return {"locked": bool(reasons), "reasons": reasons}


# ---- 에이전트 ----------------------------------------------------------------

@dataclass
class Agent:
    settings: Settings
    http: Callable[..., Any] = http_json
    clock: Callable[[], float] = time.monotonic
    _worker_cache: dict[str, Any] | None = None
    _worker_at: float = field(default=-1e9)
    _results: list[dict[str, Any]] = field(default_factory=list)

    # 관측 -------------------------------------------------------------
    def worker(self, force: bool = False) -> dict[str, Any]:
        # /status 는 무겁다(장부 집계). 11-07 부하 중 liveness 재시작이 있어 주기를 제한한다.
        if not force and self._worker_cache and \
                self.clock() - self._worker_at < self.settings.worker_status_seconds:
            return self._worker_cache
        try:
            summary = summarize_worker(self.http("GET", f"{self.settings.worker_url}/status"))
        except HttpError as exc:
            summary = unreachable(exc.code)
        self._worker_cache, self._worker_at = summary, self.clock()
        return summary

    def bot(self) -> dict[str, Any]:
        try:
            return summarize_bot(self.http("GET", f"{self.settings.bot_url}/control"),
                                 bool(self.settings.bot_token))
        except HttpError as exc:
            if exc.status == 404:
                return unreachable("NOT_SUPPORTED", "이 봇 버전에는 제어 경로가 없습니다")
            return unreachable(exc.code)

    def lock(self, worker: dict[str, Any]) -> dict[str, Any]:
        connectors, connect_error = None, None
        try:
            got = self.http("GET", f"{self.settings.connect_url}/connectors")
            connectors = [str(x) for x in got] if isinstance(got, list) else None
            if connectors is None:
                connect_error = "INVALID_RESPONSE"
        except HttpError as exc:
            connect_error = exc.code
        open_jobs, jobs_error = None, None
        try:
            open_jobs = open_cutover_jobs(self.settings.cutover_db)
        except (sqlite3.Error, OSError) as exc:
            jobs_error = type(exc).__name__
        return compute_lock(connectors=connectors, connect_error=connect_error,
                            open_jobs=open_jobs, jobs_error=jobs_error,
                            worker_state=worker.get("state"))

    # 명령 -------------------------------------------------------------
    def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        cid, target, action = command.get("id"), command.get("target"), command.get("action")
        result = {"id": cid, "ok": False}
        if target not in ("worker", "bot") or action not in ("start", "stop"):
            return {**result, "error": "INVALID_COMMAND"}
        # 서버가 확인했어도 실행 직전 상태로 다시 판단한다.
        worker = self.worker(force=True)
        lock = self.lock(worker)
        if lock["locked"]:
            return {**result, "error": "CUTOVER_LOCK: " + "; ".join(lock["reasons"])[:150]}
        enabled = action == "start"
        try:
            if target == "worker":
                if not worker.get("controllable"):
                    return {**result, "error": f"WORKER_NOT_CONTROLLABLE:{worker.get('mode')}"}
                self.http("POST", f"{self.settings.worker_url}/generation", body={"enabled": enabled})
                self.worker(force=True)
            else:
                if not self.settings.bot_token:
                    return {**result, "error": "BOT_CONTROL_TOKEN_MISSING"}
                self.http("POST", f"{self.settings.bot_url}/control/generation",
                          body={"enabled": enabled},
                          headers={"Authorization": f"Bearer {self.settings.bot_token}"})
        except HttpError as exc:
            return {**result, "error": exc.code}
        return {**result, "ok": True}

    # 서버 -------------------------------------------------------------
    def _signed(self, method: str, path: str, body: Any = None) -> Any:
        raw = b"" if body is None else json.dumps(body).encode()
        header = auth_header(self.settings.agent_key, "agent", method, path, raw)
        return self.http(method, f"{self.settings.control_url}{path}", body=body,
                         headers={"X-Demo-Auth": header})

    def step(self) -> None:
        worker = self.worker()
        bot = self.bot()
        report = {"agent": {"version": VERSION, "host": socket.gethostname()},
                  "bots": {"worker": worker, "bot": bot},
                  "lock": self.lock(worker), "results": self._results}
        self._signed("POST", "/agent/report", report)
        self._results = []
        commands = self._signed("GET", "/agent/commands").get("commands", [])
        for command in commands[:5]:
            outcome = self.execute(command)
            log(f"command {command.get('target')} {command.get('action')} -> "
                f"{'ok' if outcome['ok'] else outcome.get('error')}")
            self._results.append(outcome)


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {message}", flush=True)


def main() -> int:
    agent = Agent(Settings.from_env(dict(os.environ)))
    log(f"demo-traffic-agent v{VERSION} -> {agent.settings.control_url}")
    failures = 0
    while True:
        started = time.monotonic()
        try:
            agent.step()
            if failures:
                log("control server reachable again")
            failures = 0
        except HttpError as exc:
            failures += 1
            if failures in (1, 10) or failures % 60 == 0:
                log(f"control server error {exc.code} (x{failures})")
        time.sleep(max(0.2, agent.settings.poll_seconds - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
