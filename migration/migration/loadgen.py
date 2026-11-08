"""INSERT 워커: 초당 N건 요청을 영속 기록 후 활성 DB에 멱등 INSERT 한다. HTTP 제어 API(8080) 포함."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Protocol

import psycopg
from psycopg import sql

from . import db as dbmod
from .config import Config, ConnectionSpec, ConfigError, load_config, redact, SEED_ROWS, TOTAL_ROWS
from .state import COMMITTED, PENDING, RequestRecord, RequestStore

log = logging.getLogger("migration.loadgen")

WORKER_META_FILE = "worker.json"


class WorkerState(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    SWITCHING = "SWITCHING"
    ERROR = "ERROR"


class Outcome(str, Enum):
    INSERTED = "inserted"              # 새 행 커밋
    EXISTS_EQUAL = "exists_equal"      # 같은 ID 의 동일 행이 이미 있음 → 완료 처리
    EXISTS_DIFFERENT = "exists_different"  # 같은 ID 인데 불변 필드가 다름 → 오류


@dataclass(frozen=True)
class InsertResult:
    outcome: Outcome
    detail: str = ""


class Backend(Protocol):
    """요청을 DB 에 쓰는 대상. 테스트에서는 메모리 구현으로 대체한다."""

    name: str

    def insert(self, rec: RequestRecord) -> InsertResult: ...
    def lookup(self, request_id: str) -> tuple[Any, ...] | None: ...
    def identity(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _normalize_row(row: tuple[Any, ...]) -> tuple[str, str, int, str, str]:
    """DB 행 (run_id, phase, request_no, payload, created_at) 을 비교 가능한 형태로."""
    run_id, phase, request_no, payload, created_at = row
    if isinstance(created_at, datetime):
        created_at = created_at.astimezone(UTC).isoformat(timespec="microseconds")
    return (str(run_id), str(phase), int(request_no), str(payload), str(created_at))


def _normalize_record(rec: RequestRecord) -> tuple[str, str, int, str, str]:
    return (rec.run_id, rec.phase, rec.request_no, rec.payload,
            _parse_ts(rec.created_at).astimezone(UTC).isoformat(timespec="microseconds"))


class PgBackend:
    """psycopg 연결 한 개를 쓰는 백엔드. 동시 실행 수 1."""

    def __init__(self, spec: ConnectionSpec, table: str = "public.migration_demo") -> None:
        self.name = spec.target
        self._spec = spec
        self._table = table
        self._conn: psycopg.Connection[Any] | None = None
        self._lock = threading.Lock()

    def _connection(self) -> psycopg.Connection[Any]:
        if self._conn is None or self._conn.closed:
            self._conn = dbmod.connect(self._spec, autocommit=False, statement_timeout_ms=15_000)
            ident = dbmod.server_identity(self._conn)
            log.info("backend 연결: target=%s db=%s addr=%s", self.name, ident.database, ident.server_addr)
        return self._conn

    def _drop_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - 닫는 중 오류는 무시
                pass
            self._conn = None

    def insert(self, rec: RequestRecord) -> InsertResult:
        with self._lock:
            try:
                conn = self._connection()
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL(
                            "INSERT INTO {} (request_id, run_id, phase, request_no, payload, created_at) "
                            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (request_id) DO NOTHING RETURNING request_id"
                        ).format(dbmod.table_ident(self._table)),
                        (rec.request_id, rec.run_id, rec.phase, rec.request_no, rec.payload, _parse_ts(rec.created_at)),
                    )
                    inserted = cur.fetchone() is not None
                    if inserted:
                        conn.commit()
                        return InsertResult(Outcome.INSERTED)
                    cur.execute(
                        sql.SQL("SELECT run_id, phase, request_no, payload, created_at FROM {} WHERE request_id = %s")
                        .format(dbmod.table_ident(self._table)),
                        (rec.request_id,),
                    )
                    row = cur.fetchone()
                conn.commit()
            except psycopg.Error:
                self._drop_connection()
                raise
        return _compare_existing(rec, row)

    def lookup(self, request_id: str) -> tuple[Any, ...] | None:
        with self._lock:
            try:
                conn = self._connection()
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("SELECT run_id, phase, request_no, payload, created_at FROM {} WHERE request_id = %s")
                        .format(dbmod.table_ident(self._table)),
                        (request_id,),
                    )
                    row = cur.fetchone()
                conn.commit()
                return row
            except psycopg.Error:
                self._drop_connection()
                raise

    def identity(self) -> dict[str, Any]:
        with self._lock:
            try:
                return dbmod.server_identity(self._connection()).as_dict()
            except psycopg.Error:
                self._drop_connection()
                raise

    def close(self) -> None:
        with self._lock:
            self._drop_connection()


def _compare_existing(rec: RequestRecord, row: tuple[Any, ...] | None) -> InsertResult:
    """충돌한 기존 행과 불변 필드를 비교한다."""
    if row is None:
        # ON CONFLICT 로 걸렸지만 조회에 없다 → 경쟁 상태. 재시도하도록 일시 오류로 취급.
        raise psycopg.OperationalError("충돌 후 기존 행 조회 실패, 재시도 필요")
    if _normalize_row(row) == _normalize_record(rec):
        return InsertResult(Outcome.EXISTS_EQUAL)
    return InsertResult(Outcome.EXISTS_DIFFERENT, detail=f"existing={_normalize_row(row)!r}")


@dataclass
class WorkerSettings:
    """워커 실행 설정."""

    run_id: str
    phase: str
    target: str
    rate_per_second: float = 1.0
    state_dir: Path = field(default_factory=lambda: Path(os.environ.get("WORKER_STATE_DIR", "/var/lib/worker")))
    max_queue: int = 10_000
    max_queue_age_seconds: float = 3600.0
    max_consecutive_failures: int = 50
    max_retry_backoff_seconds: float = 30.0
    http_port: int = 8080
    http_host: str = "0.0.0.0"
    latency_window: int = 10_000
    known_targets: tuple[str, ...] = ()   # 설정 파일의 targets 이름. cutover PRECHECK 가 /status 로 확인한다
    max_generated: int | None = None


def _percentile(samples: list[float], pct: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, max(0, round(pct / 100.0 * (len(ordered) - 1))))
    return ordered[idx]


class Worker:
    """요청 생성 스레드 + 전송 스레드 + 상태 머신."""

    def __init__(
        self,
        settings: WorkerSettings,
        store: RequestStore,
        backend_factory: Callable[[str], Backend],
        *,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.store = store
        self._backend_factory = backend_factory
        self._clock = time_source
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        meta = _load_meta(settings.state_dir) or {}
        if meta.get("max_generated") is not None and meta["max_generated"] != settings.max_generated:
            raise WorkerError("cannot change a persisted demo budget; use a fresh state directory")
        if settings.max_generated is not None and meta and meta.get("run_id") != settings.run_id:
            raise WorkerError("bounded demo requires its original run_id and state directory")
        self._generation_limit = meta.get("generation_limit", 0) if settings.max_generated is not None else None
        if settings.max_generated is not None and not (
            type(self._generation_limit) is int and 0 <= self._generation_limit <= settings.max_generated
        ):
            raise WorkerError("invalid saved generation limit")
        # Operator switch for the continuous mode only; the demo budget owns generation otherwise.
        # Persisted so a liveness restart does not silently start generating again.
        self._generation_enabled = (meta.get("generation_enabled") is not False
                                    if settings.max_generated is None else None)
        self._state = WorkerState(meta.get("state", "PAUSED" if meta else "RUNNING"))
        if self._state == WorkerState.SWITCHING:
            self._state = WorkerState.PAUSED
        self._target = meta.get("target", settings.target)
        self._phase = meta.get("phase", settings.phase)
        self._unresolved = meta.get("unresolved")
        self._backend: Backend | None = None
        self._queue: deque[RequestRecord] = deque()
        self._in_flight: RequestRecord | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._next_no = store.max_request_no(settings.run_id)
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._latencies: deque[float] = deque(maxlen=settings.latency_window)
        self.counters: dict[str, int] = {
            "generated": 0, "committed_new": 0, "committed_existing": 0, "mismatch": 0,
            "queue_rejected": 0, "retries": 0, "send_errors": 0,
        }
        self.started_at = datetime.now(UTC).isoformat()
        self.last_switch: dict[str, Any] | None = None
        self._restore_pending()

    # ---- 수명 주기 -------------------------------------------------------

    def _restore_pending(self) -> None:
        pending = self.store.pending()
        if self._unresolved:
            record = self.store.get(self._unresolved)
            if record is None or record.status not in (PENDING, COMMITTED):
                raise WorkerError("unresolved request missing or mismatched; manual reconciliation required")
            if record.status == COMMITTED:
                self._unresolved = None
                self._persist_meta()
        if self._unresolved:
            pending.sort(key=lambda rec: rec.request_id != self._unresolved)
        self._queue.extend(pending)
        if pending:
            log.info("재시작: pending 요청 %d건을 같은 ID 로 이어서 처리", len(pending))

    def start(self) -> None:
        gen = threading.Thread(target=self._generator_loop, name="generator", daemon=True)
        snd = threading.Thread(target=self._sender_loop, name="sender", daemon=True)
        self._threads = [gen, snd]
        for t in self._threads:
            t.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        for t in self._threads:
            t.join(timeout)
        with self._lock:
            if self._backend is not None:
                self._backend.close()
                self._backend = None

    # ---- 상태 조회 -------------------------------------------------------

    @property
    def state(self) -> WorkerState:
        with self._lock:
            return self._state

    @property
    def target(self) -> str:
        with self._lock:
            return self._target

    @property
    def phase(self) -> str:
        with self._lock:
            return self._phase

    def queue_length(self) -> int:
        with self._lock:
            return len(self._queue)

    def status(self) -> dict[str, Any]:
        with self._lock:
            samples = list(self._latencies)
            in_flight = self._in_flight.request_id if self._in_flight else None
            oldest_age = None
            if self._queue:
                oldest_age = round(self._age_seconds(self._queue[0]), 3)
            return {
                "state": self._state.value,
                "target": self._target,
                "phase": self._phase,
                "run_id": self.settings.run_id,
                "known_targets": list(self.settings.known_targets),
                "rate_per_second": self.settings.rate_per_second,
                "generation_limit": self._generation_limit,
                "max_generated": self.settings.max_generated,
                "generation_enabled": self._generation_enabled,
                "generated_total": self._next_no,
                "queue_length": len(self._queue),
                "queue_oldest_age_seconds": oldest_age,
                "in_flight": in_flight,
                "counters": dict(self.counters),
                "store_counts": self.store.counts(),
                "committed_by_target": self.store.counts_by_target(),
                "consecutive_failures": self._consecutive_failures,
                "last_error": self._last_error,
                "latency_seconds": {
                    "p50": _percentile(samples, 50),
                    "p95": _percentile(samples, 95),
                    "max": max(samples) if samples else None,
                    "samples": len(samples),
                },
                "last_switch": self.last_switch,
                "started_at": self.started_at,
            }

    def metrics_text(self) -> str:
        st = self.status()
        lines: list[str] = []

        def gauge(name: str, value: Any, labels: str = "", help_: str = "") -> None:
            if help_:
                lines.append(f"# HELP {name} {help_}")
                lines.append(f"# TYPE {name} gauge")
            if value is None:
                return
            lines.append(f"{name}{labels} {value}")

        for state in WorkerState:
            lines.append(f'migration_worker_state{{state="{state.value}"}} {1 if st["state"] == state.value else 0}')
        lines.append(f'migration_worker_target_info{{target="{st["target"]}",phase="{st["phase"]}"}} 1')
        gauge("migration_worker_queue_length", st["queue_length"], help_="대기열 길이 (DB 쓰기 성공 아님)")
        for key, value in st["counters"].items():
            lines.append(f'migration_worker_requests_total{{kind="{key}"}} {value}')
        for key, value in st["store_counts"].items():
            lines.append(f'migration_worker_store_requests{{status="{key}"}} {value}')
        for tgt, value in st["committed_by_target"].items():
            lines.append(f'migration_worker_committed_by_target{{target="{tgt}"}} {value}')
        lat = st["latency_seconds"]
        for q in ("p50", "p95", "max"):
            if lat[q] is not None:
                lines.append(f'migration_worker_latency_seconds{{quantile="{q}"}} {lat[q]:.6f}')
        gauge("migration_worker_consecutive_failures", st["consecutive_failures"])
        return "\n".join(lines) + "\n"

    # ---- 제어 ------------------------------------------------------------

    def pause(self) -> dict[str, Any]:
        """새 요청은 대기열에만 보관하고 DB 전송을 멈춘다. 진행 중 요청은 끝까지 판정한다."""
        with self._cond:
            if self._state in (WorkerState.RUNNING, WorkerState.ERROR):
                self._state = WorkerState.PAUSED
                self._persist_meta()
                log.info("상태 → PAUSED")
            self._cond.notify_all()
        return self.status()

    def resume(self) -> dict[str, Any]:
        with self._cond:
            if self._state == WorkerState.SWITCHING:
                raise WorkerError("전환 중에는 재개할 수 없다")
            self._state = WorkerState.RUNNING
            self._consecutive_failures = 0
            self._persist_meta()
            log.info("상태 → RUNNING (target=%s)", self._target)
            self._cond.notify_all()
        return self.status()

    def drain(self, timeout: float = 60.0) -> dict[str, Any]:
        """진행 중 요청의 결과가 확정될 때까지 기다린다. 대기열은 비우지 않는다."""
        deadline = self._clock() + timeout
        with self._cond:
            while self._in_flight is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._cond.wait(min(remaining, 0.5))
            settled = self._in_flight is None and self._unresolved is None
            result = {
                "settled": settled,
                "in_flight": self._in_flight.request_id if self._in_flight else None,
                "queue_length": len(self._queue),
                "state": self._state.value,
                "last_committed": None,
            }
        last = self.store.last_committed(self.target)
        if last is not None:
            result["last_committed"] = {"request_id": last.request_id, "committed_at": last.committed_at,
                                        "target": last.target}
        return result

    def switch_target(self, new_target: str, new_phase: str | None = None) -> dict[str, Any]:
        """기존 풀을 닫고 새 target 에 연결한다. PAUSED 이고 진행 중 요청이 없을 때만 허용."""
        with self._cond:
            if self._state != WorkerState.PAUSED:
                raise WorkerError(f"target 변경은 PAUSED 상태에서만 가능 (현재 {self._state.value})")
            if self._in_flight is not None or self._unresolved is not None:
                raise WorkerError("진행 중 요청이 있다. 먼저 /drain 을 호출한다")
            self._state = WorkerState.SWITCHING
            self._persist_meta()
            old_backend, old_target = self._backend, self._target
            self._backend = None
        try:
            if old_backend is not None:
                old_backend.close()
            backend = self._backend_factory(new_target)
            identity = backend.identity()
        except Exception as exc:
            with self._cond:
                # 실패하면 target 을 바꾸지 않는다. 임의로 반대 DB 에 쓰지 않는다.
                self._backend = None
                self._state = WorkerState.PAUSED
                self._persist_meta()
                self._last_error = redact(f"target 전환 실패 ({new_target}): {exc}")
                self._cond.notify_all()
            raise WorkerError(self._last_error) from None
        with self._cond:
            self._backend = backend
            self._target = new_target
            if new_phase:
                self._phase = new_phase
            self._state = WorkerState.PAUSED
            self.last_switch = {
                "from": old_target, "to": new_target, "phase": self._phase,
                "at": datetime.now(UTC).isoformat(), "identity": identity,
            }
            self._persist_meta()
            self._cond.notify_all()
        log.info("target 전환 %s → %s (db=%s addr=%s)", old_target, new_target,
                 identity.get("database"), identity.get("server_addr"))
        return self.status()

    def _persist_meta(self) -> None:
        with self._lock:
            meta = {"target": self._target, "phase": self._phase, "run_id": self.settings.run_id,
                    "generation_limit": self._generation_limit,
                    "max_generated": self.settings.max_generated,
                    "generation_enabled": self._generation_enabled,
                    "state": self._state.value, "unresolved": self._unresolved,
                    "updated_at": datetime.now(UTC).isoformat()}
            path = self.settings.state_dir / WORKER_META_FILE
            tmp = path.with_suffix(".tmp")
            try:
                with tmp.open("w", encoding="utf-8") as stream:
                    json.dump(meta, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, path)
                fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                self._state = WorkerState.ERROR
                raise WorkerError("worker state persistence failed; sending stopped") from None

    # ---- 요청 생성 -------------------------------------------------------

    def _age_seconds(self, rec: RequestRecord) -> float:
        return (datetime.now(UTC) - _parse_ts(rec.created_at)).total_seconds()

    def _make_request(self) -> RequestRecord:
        with self._lock:
            self._next_no += 1
            no = self._next_no
            phase = self._phase
        created = datetime.now(UTC)
        return RequestRecord(
            request_id=str(uuid.uuid4()),
            run_id=self.settings.run_id,
            phase=phase,
            request_no=no,
            payload=json.dumps({"run_id": self.settings.run_id, "no": no, "phase": phase,
                                "created_at": created.isoformat()}, ensure_ascii=False, sort_keys=True),
            created_at=created.isoformat(timespec="microseconds"),
        )

    def generate_one(self) -> RequestRecord | None:
        """요청 하나를 만들어 영속 기록 후 대기열에 넣는다. 상한 초과면 거절 기록."""
        with self._lock:
            if self._generation_enabled is False:
                return None
            if self._generation_limit is not None:
                if self._next_no >= self._generation_limit or self._state != WorkerState.RUNNING:
                    return None
                # A full queue waits rather than consuming the finite demo budget.
                if len(self._queue) >= self.settings.max_queue:
                    return None
            rec = self._make_request()
            reason: str | None = None
            if len(self._queue) >= self.settings.max_queue:
                reason = f"queue_full: {len(self._queue)} >= {self.settings.max_queue}"
            elif self._queue and self._age_seconds(self._queue[0]) > self.settings.max_queue_age_seconds:
                reason = f"queue_too_old: oldest {self._age_seconds(self._queue[0]):.1f}s"
            if reason is not None:
                self.store.record_rejected(rec, reason)
                self.counters["queue_rejected"] += 1
                log.warning("요청 거절 %s: %s", rec.request_id, reason)
                return None
            self.store.record_pending(rec)
            self.counters["generated"] += 1
            self._queue.append(rec)
            self._cond.notify_all()
        return rec

    def set_generation_limit(self, limit: int) -> dict[str, Any]:
        """Absolute, persisted cap: replaying a cutover step never adds a new budget."""
        with self._lock:
            maximum = self.settings.max_generated
            if maximum is None or type(limit) is not int or not 0 <= limit <= maximum:
                raise WorkerError("generation limit outside configured demo budget")
            if limit < self._generation_limit:
                raise WorkerError("generation limit cannot decrease")
            self._generation_limit = limit
            self._persist_meta()
            return self.status()

    def set_generation_enabled(self, enabled: bool) -> dict[str, Any]:
        """Operator on/off for new requests in the continuous mode. Sending is not affected."""
        with self._lock:
            if self.settings.max_generated is not None:
                raise WorkerError("generation switch is not available with a demo budget")
            if type(enabled) is not bool:
                raise WorkerError("enabled must be a boolean")
            self._generation_enabled = enabled
            self._persist_meta()
            return self.status()

    def _generator_loop(self) -> None:
        interval = 1.0 / self.settings.rate_per_second if self.settings.rate_per_second > 0 else None
        next_at = self._clock()
        while not self._stop.is_set():
            if interval is None:
                self._stop.wait(1.0)
                continue
            now = self._clock()
            if now < next_at:
                self._stop.wait(min(next_at - now, 1.0))
                continue
            next_at += interval
            if now - next_at > interval * 10:  # 많이 뒤처졌으면 따라잡기 포기
                next_at = now + interval
            try:
                self.generate_one()
            except Exception as exc:  # noqa: BLE001 - 생성 실패는 기록하고 계속
                log.error("요청 생성 실패: %s", redact(str(exc)))

    # ---- 전송 ------------------------------------------------------------

    def _current_backend(self) -> Backend:
        with self._lock:
            if self._backend is None:
                self._backend = self._backend_factory(self._target)
            return self._backend

    def _sender_loop(self) -> None:
        while not self._stop.is_set():
            with self._cond:
                while not self._stop.is_set() and (self._state != WorkerState.RUNNING or not self._queue):
                    self._cond.wait(0.5)
                if self._stop.is_set():
                    return
                rec = self._queue.popleft()
                self._in_flight = rec
                self._unresolved = rec.request_id
                try:
                    self._persist_meta()
                except WorkerError:
                    self._queue.appendleft(rec)
                    self._in_flight = None
                    self._cond.notify_all()
                    continue
            try:
                self._send_until_settled(rec)
            except WorkerError:
                with self._cond:
                    self._state = WorkerState.ERROR
                    self._queue.appendleft(self.store.get(rec.request_id) or rec)
            finally:
                with self._cond:
                    self._in_flight = None
                    self._cond.notify_all()

    def _send_until_settled(self, rec: RequestRecord) -> None:
        """커밋 성공 또는 동일 요청의 기존 커밋을 확인할 때까지 같은 request_id 로 재시도한다."""
        attempts = rec.attempts
        backoff = 0.5
        while not self._stop.is_set():
            target = self.target
            attempts += 1
            try:
                backend = self._current_backend()
                if attempts > 1:
                    # 응답 유실 가능성: 먼저 같은 ID 로 조회해 커밋 여부를 확정한다.
                    row = backend.lookup(rec.request_id)
                    if row is not None:
                        result = _compare_existing(rec, row)
                    else:
                        result = backend.insert(rec)
                else:
                    result = backend.insert(rec)
            except Exception as exc:  # noqa: BLE001 - 연결 오류·응답 유실
                self._on_send_error(rec, target, attempts, exc)
                if getattr(exc, "sqlstate", None) == "42501":
                    # Server rejected this write. Reconcile any earlier lost response
                    # before declaring the request safe to carry to another target.
                    try:
                        row = self._current_backend().lookup(rec.request_id)
                    except Exception:
                        row = False  # unknown: keep the unresolved fence
                    if row is not False:
                        if row is not None:
                            self._on_send_result(rec, target, attempts, _compare_existing(rec, row))
                            return
                        with self._cond:
                            self._unresolved = None
                            self._state = WorkerState.PAUSED
                            self._persist_meta()
                            self._queue.appendleft(RequestRecord(**{**rec.__dict__, "attempts": attempts}))
                        return
                if self._state == WorkerState.ERROR:
                    with self._cond:
                        self._queue.appendleft(RequestRecord(**{**rec.__dict__, "attempts": attempts}))
                    return
                self._stop.wait(backoff)
                backoff = min(backoff * 2, self.settings.max_retry_backoff_seconds)
                continue
            self._on_send_result(rec, target, attempts, result)
            return

    def _on_send_error(self, rec: RequestRecord, target: str, attempts: int, exc: Exception) -> None:
        msg = redact(f"{type(exc).__name__}: {exc}")[:500]
        with self._lock:
            self.counters["send_errors"] += 1
            self.counters["retries"] += 1  # 실패한 시도마다 같은 ID 로 한 번 더 보낸다
            self._consecutive_failures += 1
            self._last_error = msg
            if (self.settings.max_consecutive_failures > 0
                    and self._consecutive_failures >= self.settings.max_consecutive_failures
                    and self._state == WorkerState.RUNNING):
                self._state = WorkerState.ERROR
                self._persist_meta()
                log.error("연속 실패 %d회 → ERROR. 반대 DB 에 쓰지 않고 중단한다. /resume 으로 재개",
                          self._consecutive_failures)
        self.store.record_attempt(rec.request_id, target, attempts, msg)
        log.warning("전송 실패 request_id=%s target=%s attempts=%d: %s", rec.request_id, target, attempts, msg)

    def _on_send_result(self, rec: RequestRecord, target: str, attempts: int, result: InsertResult) -> None:
        now = datetime.now(UTC)
        with self._lock:
            self._consecutive_failures = 0
        if result.outcome == Outcome.EXISTS_DIFFERENT:
            self.store.mark_mismatch(rec.request_id, target, attempts, result.detail)
            with self._lock:
                self.counters["mismatch"] += 1
                self._last_error = f"mismatch request_id={rec.request_id}"
            log.error("불변 필드 불일치 request_id=%s target=%s %s", rec.request_id, target, result.detail)
            return
        committed_at = now.isoformat(timespec="microseconds")
        self.store.mark_committed(rec.request_id, target, attempts, committed_at)
        latency = (now - _parse_ts(rec.created_at)).total_seconds()
        with self._lock:
            self._unresolved = None
            self._last_error = None
            self._latencies.append(latency)
            if result.outcome == Outcome.INSERTED:
                self.counters["committed_new"] += 1
            else:
                self.counters["committed_existing"] += 1
            self._persist_meta()


class WorkerError(RuntimeError):
    """제어 API 에서 거부되는 요청."""


# ---- HTTP 제어 API -------------------------------------------------------


def make_handler(worker: Worker) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "migration-worker/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 - 표준 핸들러 시그니처
            log.debug("http %s", fmt % args)

        def _send_json(self, code: int, body: Any) -> None:
            data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_text(self, code: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
            data = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(min(length, 65536))
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkerError(f"JSON 본문 파싱 실패: {exc}") from None
            if not isinstance(body, dict):
                raise WorkerError("JSON 본문은 객체여야 한다")
            return body

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._send_json(200, {"ok": True, "state": worker.state.value})
            elif self.path == "/status":
                self._send_json(200, worker.status())
            elif self.path == "/metrics":
                self._send_text(200, worker.metrics_text(), "text/plain; version=0.0.4; charset=utf-8")
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                if self.path == "/pause":
                    self._send_json(200, worker.pause())
                elif self.path == "/resume":
                    self._send_json(200, worker.resume())
                elif self.path == "/generation-limit":
                    self._send_json(200, worker.set_generation_limit(self._read_json().get("limit")))
                elif self.path == "/generation":
                    self._send_json(200, worker.set_generation_enabled(self._read_json().get("enabled")))
                elif self.path == "/drain":
                    body = self._read_json()
                    timeout = float(body.get("timeout", 60))
                    result = worker.drain(timeout=timeout)
                    self._send_json(200 if result["settled"] else 409, result)
                elif self.path == "/target":
                    body = self._read_json()
                    target = body.get("target")
                    if not isinstance(target, str) or not target:
                        raise WorkerError("body.target 이 필요하다")
                    phase = body.get("phase")
                    if phase is not None and not isinstance(phase, str):
                        raise WorkerError("body.phase 는 문자열이어야 한다")
                    self._send_json(200, worker.switch_target(target, phase))
                else:
                    self._send_json(404, {"error": "not found"})
            except WorkerError as exc:
                self._send_json(409, {"error": str(exc), "state": worker.state.value})
            except Exception as exc:  # noqa: BLE001
                log.exception("제어 API 오류")
                self._send_json(500, {"error": redact(f"{type(exc).__name__}: {exc}")})

    return Handler


def serve(worker: Worker, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(worker))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    thread.start()
    return server


# ---- CLI ---------------------------------------------------------------------


def _load_meta(state_dir: Path) -> dict[str, Any] | None:
    path = state_dir / WORKER_META_FILE
    if not path.exists():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict) or not all(isinstance(meta.get(k), str) and meta[k]
                                               for k in ("target", "phase", "run_id")):
            raise ValueError("invalid metadata")
        WorkerState(meta.get("state", "PAUSED"))
        if meta.get("unresolved") is not None and not isinstance(meta["unresolved"], str):
            raise ValueError("invalid unresolved request")
        if meta.get("generation_enabled") is not None and not isinstance(meta["generation_enabled"], bool):
            raise ValueError("invalid generation switch")
        return meta
    except (OSError, ValueError, TypeError):
        raise WorkerError("worker.json invalid; refusing to resume automatically") from None


def build_settings(args: argparse.Namespace, config: Config) -> WorkerSettings:
    """CLI·설정 파일·저장된 메타데이터로 워커 설정을 만든다. 저장된 target 이 CLI 보다 우선한다."""
    state_dir = Path(args.state_dir or os.environ.get("WORKER_STATE_DIR") or config.worker.state_dir)
    meta = _load_meta(state_dir)
    target = args.target or config.job.source
    phase = args.phase or config.job.live_phase_before
    if meta and meta.get("target"):
        if args.target and args.target != meta["target"]:
            log.warning("저장된 target=%s 이 CLI --target=%s 보다 우선한다 (전환 후 재시작 보호)",
                        meta["target"], args.target)
        target = str(meta["target"])
        phase = str(meta.get("phase") or phase)
    run_id = args.run_id or (meta or {}).get("run_id") or f"{config.job.job_id}-live"
    return WorkerSettings(
        run_id=str(run_id),
        phase=phase,
        target=target,
        rate_per_second=args.rate,
        state_dir=state_dir,
        known_targets=tuple(config.targets),
        max_generated=TOTAL_ROWS - SEED_ROWS,  # 1000건 예산: 봇이 만들 수 있는 요청은 100 + 100 = 200 뿐
        max_queue=args.max_queue,
        max_queue_age_seconds=args.max_queue_age,
        max_consecutive_failures=args.max_consecutive_failures,
        max_retry_backoff_seconds=args.max_retry_backoff,
        http_port=args.port,
        http_host=args.host,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="INSERT 워커 (초당 N건, 영속 요청 기록, HTTP 제어 API)")
    p.add_argument("--config", default=os.environ.get("MIGRATION_CONFIG", "config.yaml"))
    p.add_argument("--target", default=os.environ.get("WORKER_TARGET"), help="초기 target 이름 (저장된 값이 우선)")
    p.add_argument("--phase", default=os.environ.get("WORKER_PHASE"))
    p.add_argument("--run-id", default=os.environ.get("WORKER_RUN_ID"))
    p.add_argument("--rate", type=float, default=float(os.environ.get("WORKER_RATE", "1")))
    p.add_argument("--state-dir", default=None)
    p.add_argument("--port", type=int, default=int(os.environ.get("WORKER_PORT", "8080")))
    p.add_argument("--host", default=os.environ.get("WORKER_HOST", "0.0.0.0"))
    p.add_argument("--max-queue", type=int, default=int(os.environ.get("WORKER_MAX_QUEUE", "10000")))
    p.add_argument("--max-queue-age", type=float, default=float(os.environ.get("WORKER_MAX_QUEUE_AGE", "3600")))
    p.add_argument("--max-consecutive-failures", type=int,
                   default=int(os.environ.get("WORKER_MAX_CONSECUTIVE_FAILURES", "50")))
    p.add_argument("--max-retry-backoff", type=float, default=30.0)
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = load_config(args.config)
        settings = build_settings(args, config)
        config.target(settings.target)
    except ConfigError as exc:
        log.error("설정 오류: %s", exc)
        return 2
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    store = RequestStore(settings.state_dir)

    def factory(name: str) -> Backend:
        return PgBackend(config.connection(name, "app", application_name="migration-worker"), config.job.table)

    worker = Worker(settings, store, factory)
    worker._persist_meta()
    server = serve(worker, settings.http_host, settings.http_port)
    worker.start()
    log.info("워커 시작 target=%s phase=%s rate=%s/s state_dir=%s http=%s:%d",
             settings.target, settings.phase, settings.rate_per_second, settings.state_dir,
             settings.http_host, settings.http_port)

    stop = threading.Event()

    def _signal(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)
    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        log.info("종료 중: 진행 중 요청 판정 후 정지")
        server.shutdown()
        worker.stop()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
