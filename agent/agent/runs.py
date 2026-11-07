"""실행 기록 (sqlite). 제안·판정 코드·상태 전이를 남긴다. 저장소 값 전체나 비밀값은 넣지 않는다."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

RUNS_DB_FILE = "agent-runs.sqlite3"

CHECKING = "CHECKING"
NOTHING_TO_FIX = "NOTHING_TO_FIX"
HANDED_OFF = "HANDED_OFF"
REJECTED = "REJECTED"
PROBE_FAILED = "PROBE_FAILED"
WAITING_APPROVAL = "WAITING_APPROVAL"
APPLYING = "APPLYING"
CAS_CONFLICT = "CAS_CONFLICT"
RESTART_FAILED = "RESTART_FAILED"
RESOLVED = "RESOLVED"
UNRESOLVED = "UNRESOLVED"
ERROR = "ERROR"

# 프로세스 종료 코드 0 인 끝 상태. 나머지는 사람이 봐야 한다.
OK_STATES = frozenset({NOTHING_TO_FIX, WAITING_APPROVAL, RESOLVED})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    job_id     TEXT NOT NULL,
    state      TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL REFERENCES runs(run_id),
    at      TEXT NOT NULL,
    state   TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Run:
    run_id: str
    job_id: str
    state: str
    detail: dict[str, Any]
    created_at: str
    updated_at: str


class RunStore:
    def __init__(self, state_dir: str | Path) -> None:
        directory = Path(state_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(directory / RUNS_DB_FILE)
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def create(self, job_id: str) -> Run:
        at = _now()
        run = Run(f"run-{uuid.uuid4().hex[:12]}", job_id, CHECKING, {}, at, at)
        with self._conn:
            self._conn.execute(
                "INSERT INTO runs (run_id, job_id, state, detail, created_at, updated_at) VALUES (?, ?, ?, '{}', ?, ?)",
                (run.run_id, job_id, CHECKING, at, at),
            )
            self._event(run.run_id, CHECKING, "실행 시작", at)
        return run

    def _event(self, run_id: str, state: str, message: str, at: str) -> None:
        self._conn.execute(
            "INSERT INTO run_events (run_id, at, state, message) VALUES (?, ?, ?, ?)", (run_id, at, state, message)
        )

    def update(self, run: Run, state: str, message: str, **detail: Any) -> None:
        at = _now()
        run.state, run.updated_at = state, at
        run.detail.update(detail)
        with self._conn:
            self._conn.execute(
                "UPDATE runs SET state = ?, detail = ?, updated_at = ? WHERE run_id = ?",
                (state, json.dumps(run.detail, ensure_ascii=False), at, run.run_id),
            )
            self._event(run.run_id, state, message, at)

    def claim_approval(self, run_id: str) -> bool:
        """WAITING_APPROVAL → APPLYING 원자 전이. 승인 명령을 두 번 실행해도 한 번만 적용한다."""
        at = _now()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ? AND state = ?",
                (APPLYING, at, run_id, WAITING_APPROVAL),
            )
            if cur.rowcount != 1:
                return False
            self._event(run_id, APPLYING, "승인됨", at)
        return True

    def get(self, run_id: str) -> Run | None:
        row = self._conn.execute(
            "SELECT run_id, job_id, state, detail, created_at, updated_at FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._to_run(row) if row else None

    def recent(self, limit: int = 20) -> list[Run]:
        rows = self._conn.execute(
            "SELECT run_id, job_id, state, detail, created_at, updated_at FROM runs ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._to_run(row) for row in rows]

    def events(self, run_id: str) -> list[Mapping[str, str]]:
        rows = self._conn.execute(
            "SELECT at, state, message FROM run_events WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [{"at": a, "state": s, "message": m} for a, s, m in rows]

    @staticmethod
    def _to_run(row: tuple[Any, ...]) -> Run:
        return Run(row[0], row[1], row[2], json.loads(row[3]), row[4], row[5])
