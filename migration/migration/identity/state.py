"""Separate durable identity journal and process lock (no database state imports)."""
from __future__ import annotations
import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from .models import ConflictError, canonical, reject_secrets


class State:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / "identity.sqlite3"
        if self.path.is_symlink():
            raise ConflictError("state database must not be a symbolic link")
        self.db = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS runs (plan TEXT PRIMARY KEY, review TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS operations (plan TEXT, id TEXT, result TEXT NOT NULL, PRIMARY KEY(plan,id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS verification (plan TEXT PRIMARY KEY, result TEXT NOT NULL)")

    @contextmanager
    def lock(self):
        fd = os.open(self.directory / ".identity.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConflictError("another identity execution holds this state directory") from None
            yield
        finally:
            os.close(fd)

    def bind(self, plan: str, review: dict):
        reject_secrets(review)
        rows = self.db.execute("SELECT plan, review FROM runs").fetchall()
        if rows and (rows[0][0] != plan or rows[0][1] != canonical(review)):
            raise ConflictError("state directory belongs to a different plan or review")
        self.db.execute("INSERT OR IGNORE INTO runs VALUES (?,?)", (plan, canonical(review)))

    def record(self, plan: str, operation_id: str, result: dict):
        reject_secrets(result)
        self.db.execute("INSERT OR REPLACE INTO operations VALUES (?,?,?)", (plan, operation_id, canonical(result)))

    def get(self, plan: str, operation_id: str) -> dict | None:
        row = self.db.execute("SELECT result FROM operations WHERE plan=? AND id=?", (plan, operation_id)).fetchone()
        return json.loads(row[0]) if row else None

    def results(self, plan: str | None = None) -> list[dict]:
        rows = self.db.execute("SELECT plan,id,result FROM operations ORDER BY plan,id").fetchall()
        return [{"plan_digest": p, "operation_id": i, **json.loads(r)} for p, i, r in rows if plan is None or p == plan]

    def record_verification(self, plan: str, result: dict):
        reject_secrets(result)
        self.db.execute("INSERT OR REPLACE INTO verification VALUES (?,?)", (plan, canonical(result)))

    def verification(self, plan: str) -> dict | None:
        row = self.db.execute("SELECT result FROM verification WHERE plan=?", (plan,)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
