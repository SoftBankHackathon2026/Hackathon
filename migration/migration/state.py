"""SQLite 기반 영속 상태: 워커 요청 기록과 전환 단계 체크포인트."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

REQUEST_DB_FILE = "requests.sqlite3"
CUTOVER_DB_FILE = "cutover.sqlite3"

# 요청 상태
PENDING = "pending"            # 영속 기록만 됨. DB 쓰기 성공이 아니다.
COMMITTED = "committed"        # 커밋 성공 또는 동일 요청의 기존 커밋 확인
MISMATCH = "mismatch"          # 같은 request_id 인데 불변 필드가 다름
QUEUE_REJECTED = "queue_rejected"  # 대기열 상한 초과로 생성 실패 기록

_SCHEMA_REQUESTS = """
CREATE TABLE IF NOT EXISTS requests (
    request_id   TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    phase        TEXT NOT NULL,
    request_no   INTEGER NOT NULL,
    payload      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL,
    target       TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    committed_at TEXT,
    last_error   TEXT,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_status_idx ON requests(status, created_at);
CREATE INDEX IF NOT EXISTS requests_target_idx ON requests(target, status);
"""

_SCHEMA_CUTOVER = """
CREATE TABLE IF NOT EXISTS cutover_jobs (
    job_id      TEXT PRIMARY KEY,
    direction   TEXT NOT NULL,
    source      TEXT NOT NULL,
    target      TEXT NOT NULL,
    stage       TEXT NOT NULL,
    checkpoint  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cutover_steps (
    job_id       TEXT NOT NULL,
    step         TEXT NOT NULL,
    detail       TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    PRIMARY KEY (job_id, step)
);
"""


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class RequestRecord:
    """워커가 만든 요청 하나. 불변 필드는 최초 생성 시 고정된다."""

    request_id: str
    run_id: str
    phase: str
    request_no: int
    payload: str
    created_at: str  # ISO-8601 UTC
    status: str = PENDING
    target: str | None = None
    attempts: int = 0
    committed_at: str | None = None
    last_error: str | None = None

    def immutable_fields(self) -> tuple[str, str, int, str, str]:
        return (self.run_id, self.phase, self.request_no, self.payload, self.created_at)


class _SqliteBase:
    """스레드 안전한 단일 연결 SQLite 래퍼."""

    def __init__(self, path: Path, schema: str) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(schema)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(query, params)


def _created_in_scope(created: str, since: datetime | None, upto: datetime | None) -> bool:
    ts = datetime.fromisoformat(created)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    if since is not None and ts <= since:
        return False
    return not (upto is not None and ts > upto)


class RequestStore(_SqliteBase):
    """워커 요청 기록. 요청은 DB 전송 전에 pending 으로 먼저 저장한다."""

    def __init__(self, state_dir: str | Path) -> None:
        super().__init__(Path(state_dir) / REQUEST_DB_FILE, _SCHEMA_REQUESTS)

    def record_pending(self, rec: RequestRecord) -> bool:
        """새 요청을 pending 으로 저장. 이미 있으면 False (같은 ID 재사용)."""
        cur = self._exec(
            "INSERT OR IGNORE INTO requests (request_id, run_id, phase, request_no, payload, created_at, "
            "status, target, attempts, committed_at, last_error, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL, ?)",
            (rec.request_id, rec.run_id, rec.phase, rec.request_no, rec.payload, rec.created_at, PENDING, utcnow_iso()),
        )
        return cur.rowcount == 1

    def record_rejected(self, rec: RequestRecord, reason: str) -> None:
        """대기열 상한 초과 등으로 요청 생성 자체가 실패했음을 남긴다. 조용히 버리지 않는다."""
        self._exec(
            "INSERT OR REPLACE INTO requests (request_id, run_id, phase, request_no, payload, created_at, "
            "status, target, attempts, committed_at, last_error, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, ?, ?)",
            (rec.request_id, rec.run_id, rec.phase, rec.request_no, rec.payload, rec.created_at,
             QUEUE_REJECTED, reason, utcnow_iso()),
        )

    def mark_committed(self, request_id: str, target: str, attempts: int, committed_at: str | None = None) -> None:
        self._exec(
            "UPDATE requests SET status = ?, target = ?, attempts = ?, committed_at = ?, last_error = NULL, "
            "updated_at = ? WHERE request_id = ?",
            (COMMITTED, target, attempts, committed_at or utcnow_iso(), utcnow_iso(), request_id),
        )

    def mark_mismatch(self, request_id: str, target: str, attempts: int, detail: str) -> None:
        self._exec(
            "UPDATE requests SET status = ?, target = ?, attempts = ?, last_error = ?, updated_at = ? "
            "WHERE request_id = ?",
            (MISMATCH, target, attempts, detail, utcnow_iso(), request_id),
        )

    def record_attempt(self, request_id: str, target: str, attempts: int, error: str | None) -> None:
        """재시도 횟수와 마지막 오류를 갱신한다 (상태는 pending 유지)."""
        self._exec(
            "UPDATE requests SET target = ?, attempts = ?, last_error = ?, updated_at = ? WHERE request_id = ?",
            (target, attempts, error, utcnow_iso(), request_id),
        )

    def get(self, request_id: str) -> RequestRecord | None:
        cur = self._exec("SELECT * FROM requests WHERE request_id = ?", (request_id,))
        row = cur.fetchone()
        return _row_to_record(cur, row) if row else None

    def pending(self) -> list[RequestRecord]:
        """재시작 시 이어서 처리할 요청. 생성 순서대로."""
        cur = self._exec(
            "SELECT * FROM requests WHERE status = ? ORDER BY created_at, request_no", (PENDING,)
        )
        return [_row_to_record(cur, row) for row in cur.fetchall()]

    def committed_ids(self, target: str | None = None) -> set[str]:
        """커밋 확인된 요청 ID 집합 S. target 을 주면 그 DB 에서 커밋된 것만."""
        if target is None:
            cur = self._exec("SELECT request_id FROM requests WHERE status = ?", (COMMITTED,))
        else:
            cur = self._exec("SELECT request_id FROM requests WHERE status = ? AND target = ?", (COMMITTED, target))
        return {row[0] for row in cur.fetchall()}

    def iter_committed_ids_in_scope(self, target: str | None = None, *, since: datetime | None = None,
                                    upto: datetime | None = None, batch: int = 5000) -> Iterator[list[str]]:
        """created_at 이 (since, upto] 범위인 커밋 확인 요청 ID 를 request_id 순으로 batch 개씩 낸다.

        전체 집합을 메모리에 올리지 않는다. 페이지마다 새 조회(keyset: request_id > 마지막 ID, PK 인덱스)를 쓰므로
        읽기 트랜잭션을 길게 잡지 않아 같은 SQLite 에 쓰는 워커를 막지 않는다.
        created_at 은 ISO 문자열이라 형식 차이(마이크로초 생략 등)에 흔들리지 않게 파싱해서 비교한다.
        """
        last = ""
        while True:
            if target is None:
                cur = self._exec("SELECT request_id, created_at FROM requests WHERE status = ? AND request_id > ? "
                                 "ORDER BY request_id LIMIT ?", (COMMITTED, last, batch))
            else:
                cur = self._exec("SELECT request_id, created_at FROM requests WHERE status = ? AND target = ? "
                                 "AND request_id > ? ORDER BY request_id LIMIT ?", (COMMITTED, target, last, batch))
            rows = cur.fetchall()
            if not rows:
                return
            last = str(rows[-1][0])
            page = [str(rid) for rid, created in rows if _created_in_scope(str(created), since, upto)]
            if page:
                yield page
            if len(rows) < batch:
                return

    def committed_ids_in_scope(self, target: str | None = None, *, since: datetime | None = None,
                               upto: datetime | None = None) -> set[str]:
        """iter_committed_ids_in_scope 를 한 집합으로 모은 것. 행 전체를 읽는 rows 비교처럼 어차피 메모리에 올리는 경로에서만 쓴다."""
        if since is None and upto is None:
            return self.committed_ids(target)
        out: set[str] = set()
        for page in self.iter_committed_ids_in_scope(target, since=since, upto=upto):
            out.update(page)
        return out

    def iter_committed(self, target: str | None = None) -> Iterator[RequestRecord]:
        if target is None:
            cur = self._exec("SELECT * FROM requests WHERE status = ? ORDER BY committed_at", (COMMITTED,))
        else:
            cur = self._exec(
                "SELECT * FROM requests WHERE status = ? AND target = ? ORDER BY committed_at", (COMMITTED, target)
            )
        for row in cur.fetchall():
            yield _row_to_record(cur, row)

    def last_committed(self, target: str | None = None) -> RequestRecord | None:
        """가장 최근에 커밋 확인된 요청 (전환 체크포인트용)."""
        if target is None:
            cur = self._exec(
                "SELECT * FROM requests WHERE status = ? ORDER BY committed_at DESC LIMIT 1", (COMMITTED,)
            )
        else:
            cur = self._exec(
                "SELECT * FROM requests WHERE status = ? AND target = ? ORDER BY committed_at DESC LIMIT 1",
                (COMMITTED, target),
            )
        row = cur.fetchone()
        return _row_to_record(cur, row) if row else None

    def counts(self) -> dict[str, int]:
        cur = self._exec("SELECT status, count(*) FROM requests GROUP BY status")
        out = {PENDING: 0, COMMITTED: 0, MISMATCH: 0, QUEUE_REJECTED: 0}
        for status, n in cur.fetchall():
            out[status] = int(n)
        return out

    def counts_by_target(self) -> dict[str, int]:
        cur = self._exec(
            "SELECT coalesce(target, ''), count(*) FROM requests WHERE status = ? GROUP BY target", (COMMITTED,)
        )
        return {str(t): int(n) for t, n in cur.fetchall()}

    def max_request_no(self, run_id: str) -> int:
        cur = self._exec("SELECT coalesce(max(request_no), 0) FROM requests WHERE run_id = ?", (run_id,))
        row = cur.fetchone()
        return int(row[0]) if row else 0


def _row_to_record(cur: sqlite3.Cursor, row: tuple[Any, ...]) -> RequestRecord:
    cols = [d[0] for d in cur.description]
    data = dict(zip(cols, row, strict=True))
    return RequestRecord(
        request_id=data["request_id"],
        run_id=data["run_id"],
        phase=data["phase"],
        request_no=int(data["request_no"]),
        payload=data["payload"],
        created_at=data["created_at"],
        status=data["status"],
        target=data["target"],
        attempts=int(data["attempts"]),
        committed_at=data["committed_at"],
        last_error=data["last_error"],
    )


@dataclass(frozen=True)
class CutoverJob:
    """전환 작업의 저장된 상태."""

    job_id: str
    direction: str
    source: str
    target: str
    stage: str
    checkpoint: dict[str, Any]
    updated_at: str


class CutoverStore(_SqliteBase):
    """전환 단계와 체크포인트를 영속화한다. 재실행 시 저장된 단계부터 재개한다."""

    def __init__(self, state_dir: str | Path) -> None:
        super().__init__(Path(state_dir) / CUTOVER_DB_FILE, _SCHEMA_CUTOVER)

    def load(self, job_id: str) -> CutoverJob | None:
        cur = self._exec("SELECT * FROM cutover_jobs WHERE job_id = ?", (job_id,))
        row = cur.fetchone()
        if not row:
            return None
        return CutoverJob(
            job_id=row[0], direction=row[1], source=row[2], target=row[3], stage=row[4],
            checkpoint=json.loads(row[5]), updated_at=row[6],
        )

    def save(self, job: CutoverJob) -> None:
        self._exec(
            "INSERT INTO cutover_jobs (job_id, direction, source, target, stage, checkpoint, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET "
            "stage = excluded.stage, checkpoint = excluded.checkpoint, updated_at = excluded.updated_at",
            (job.job_id, job.direction, job.source, job.target, job.stage,
             json.dumps(job.checkpoint, ensure_ascii=False, sort_keys=True), utcnow_iso()),
        )

    def step_done(self, job_id: str, step: str) -> dict[str, Any] | None:
        """이미 수행한 외부 작업이면 그 기록을 돌려준다 (재실행 금지 판단용)."""
        cur = self._exec("SELECT detail FROM cutover_steps WHERE job_id = ? AND step = ?", (job_id, step))
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def mark_step(self, job_id: str, step: str, detail: dict[str, Any] | None = None) -> None:
        self._exec(
            "INSERT OR REPLACE INTO cutover_steps (job_id, step, detail, completed_at) VALUES (?, ?, ?, ?)",
            (job_id, step, json.dumps(detail or {}, ensure_ascii=False, sort_keys=True, default=str), utcnow_iso()),
        )

    def steps(self, job_id: str) -> list[tuple[str, str]]:
        cur = self._exec(
            "SELECT step, completed_at FROM cutover_steps WHERE job_id = ? ORDER BY completed_at", (job_id,)
        )
        return [(str(s), str(t)) for s, t in cur.fetchall()]
