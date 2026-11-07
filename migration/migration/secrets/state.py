"""Private metadata-only journal. Every external write starts with a durable intent.

A private per-user destination lock coordinates every state path on one execution
host. File locks do not coordinate execution hosts; distributed runs are blocked
by operational policy. No payload, value digest, credential or raw error field exists.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path
from typing import Iterator


class StateError(RuntimeError):
    """A fixed error code safe for logs and reports."""


def _text(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 2048
        or any(ord(c) < 32 for c in value)
    ):
        raise StateError("STATE_METADATA_INVALID")
    return value


REASON_CODES = frozenset(
    {
        "WRITE_RESPONSE_UNKNOWN",
        "WRITE_PROVENANCE_UNKNOWN",
        "WRITE_UNKNOWN",
        "UNKNOWN",
        "PREPARE_UNCONFIRMED",
        "REFERENCE_RESPONSE_UNKNOWN",
        "ROTATION_WRITE_UNKNOWN",
        "ROTATION_EXTERNAL_UNCONFIRMED",
        "DELETE_BLOCK_UNCONFIRMED",
        "DELETE_RESTORE_UNCONFIRMED",
        "DELETE_DESTROY_UNCONFIRMED",
    }
)


def _code(value: str) -> str:
    if type(value) is not str or value not in REASON_CODES:
        raise StateError("STATE_REASON_INVALID")
    return value


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.name in {"requests.sqlite3", "cutover.sqlite3", "policy.sqlite3"}:
            raise StateError("STATE_PATH_RESERVED")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise StateError("STATE_PATH_UNSAFE")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise StateError("STATE_PATH_UNSAFE")
        finally:
            os.close(fd)
        self.db = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        existing = {
            r[0]
            for r in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if existing - {
            "job",
            "operations",
            "plan_history",
            "installation_checks",
            "sqlite_sequence",
        }:
            self.db.close()
            raise StateError("STATE_SCHEMA_MISMATCH")
        os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS job (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            schema_version INTEGER NOT NULL DEFAULT 1,
            run_id TEXT NOT NULL, plan_digest TEXT NOT NULL,
            source_identity TEXT NOT NULL, target_identity TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'planned',
            last_stable_stage TEXT NOT NULL DEFAULT 'planned'
        );
        CREATE TABLE IF NOT EXISTS installation_checks (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            state TEXT NOT NULL CHECK(state IN ('ready','needs_unseal','needs_credentials','needs_recovery','blocked','failed')),
            checked_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plan_history (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            old_digest TEXT NOT NULL, new_digest TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS operations (
            operation_id TEXT PRIMARY KEY,
            source_resource TEXT NOT NULL, source_version TEXT NOT NULL,
            target_resource TEXT NOT NULL, transform TEXT NOT NULL,
            before_versions TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('intent','unknown','complete')),
            target_version TEXT, verified INTEGER NOT NULL DEFAULT 0,
            reason_code TEXT,
            UNIQUE(source_resource,source_version,transform,target_resource)
        );
        """)
        if "last_stable_stage" not in {
            r[1] for r in self.db.execute("PRAGMA table_info(job)")
        }:
            self.db.execute(
                "ALTER TABLE job ADD COLUMN last_stable_stage TEXT NOT NULL DEFAULT 'planned'"
            )
            self.db.execute(
                "UPDATE job SET last_stable_stage=stage WHERE stage NOT IN ('blocked','needs_recovery')"
            )
        self._lock_active = False
        self._locked_identity: str | None = None

    def note_installation_state(self, state: str) -> None:
        self._writing()
        if type(state) is not str or state not in {
            "ready",
            "needs_unseal",
            "needs_credentials",
            "needs_recovery",
            "blocked",
            "failed",
        }:
            raise StateError("STATE_METADATA_INVALID")
        self.db.execute(
            "INSERT INTO installation_checks(singleton,state,checked_at) VALUES(1,?,?) "
            "ON CONFLICT(singleton) DO UPDATE SET state=excluded.state,checked_at=excluded.checked_at",
            (state, time.time()),
        )

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> State:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @contextlib.contextmanager
    def lock(self, target_identity: str) -> Iterator[None]:
        identity = _text(target_identity)
        if self._lock_active:
            raise StateError("STATE_ALREADY_LOCKED")
        # A scoped path identity must still exclude a legacy unscoped writer on
        # the same mount: both could otherwise mutate the same logical key.
        parts = identity.split(":", 4)
        lock_identity = (
            ":".join(parts[:4])
            if parts[0] in {"vault", "openbao"} and len(parts) >= 4
            else identity
        )
        suffix = hashlib.sha256(lock_identity.encode()).hexdigest()[:24]
        # Share destination locks across arbitrary state directories on this host.
        # Refuse symlinks, wrong ownership and public directories before opening.
        lock_root = Path("/tmp") / ("secrets-migration-locks-" + str(os.getuid()))
        lock_root.mkdir(mode=0o700, exist_ok=True)
        info = lock_root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise StateError("STATE_LOCK_PATH_UNSAFE")
        # DB lock also excludes two jobs using the same DB with different targets.
        names = [
            self.path.with_suffix(self.path.suffix + ".lock"),
            lock_root / ("target-" + suffix + ".lock"),
        ]
        fds: list[int] = []
        try:
            for name in names:
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                fds.append(fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise StateError("STATE_LOCKED") from None
            self._lock_active = True
            self._locked_identity = identity
            yield
        finally:
            self._lock_active = False
            self._locked_identity = None
            for fd in reversed(fds):
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _writing(self) -> None:
        if not self._lock_active:
            raise StateError("STATE_LOCK_REQUIRED")
        row = self.db.execute(
            "SELECT target_identity FROM job WHERE singleton=1"
        ).fetchone()
        if row is not None and row[0] != self._locked_identity:
            raise StateError("STATE_LOCK_TARGET_MISMATCH")

    def bind(
        self, run_id: str, plan_digest: str, source_identity: str, target_identity: str
    ) -> None:
        self._writing()
        values = tuple(
            _text(x) for x in (run_id, plan_digest, source_identity, target_identity)
        )
        if target_identity != self._locked_identity:
            raise StateError("STATE_LOCK_TARGET_MISMATCH")
        if source_identity == target_identity:
            raise StateError("STATE_IDENTITIES_EQUAL")
        row = self.db.execute(
            "SELECT run_id,plan_digest,source_identity,target_identity FROM job"
        ).fetchone()
        if row is not None:
            if tuple(row) != values:
                raise StateError("STATE_PLAN_CHANGED")
            return
        self.db.execute(
            "INSERT INTO job(singleton,run_id,plan_digest,source_identity,target_identity) VALUES(1,?,?,?,?)",
            values,
        )

    def amend_plan(self, expected_digest: str, new_digest: str) -> None:
        """Record an authorized final-sync plan extension, retaining old evidence.

        The engine must separately verify frozen source, unchanged configuration,
        identities, scope and a newly allowed plan before calling this method.
        """
        self._writing()
        expected_digest, new_digest = _text(expected_digest), _text(new_digest)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT plan_digest FROM job WHERE singleton=1"
            ).fetchone()
            if row is None or row[0] != expected_digest:
                raise StateError("STATE_PLAN_CHANGED")
            if new_digest != expected_digest:
                self.db.execute(
                    "INSERT INTO plan_history(old_digest,new_digest) VALUES(?,?)",
                    (expected_digest, new_digest),
                )
                self.db.execute(
                    "UPDATE job SET plan_digest=? WHERE singleton=1", (new_digest,)
                )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def intent(
        self,
        operation_id: str,
        source_resource: str,
        source_version: str,
        target_resource: str,
        transform: str = "identity-v1",
        before_versions: tuple[str, ...] = (),
    ) -> None:
        self._writing()
        fields = tuple(
            _text(v)
            for v in (
                operation_id,
                source_resource,
                source_version,
                target_resource,
                transform,
            )
        )
        if type(before_versions) not in (tuple, list):
            raise StateError("STATE_METADATA_INVALID")
        before = json.dumps([_text(v) for v in before_versions], separators=(",", ":"))
        if self.db.execute("SELECT 1 FROM job").fetchone() is None:
            raise StateError("STATE_NOT_BOUND")
        previous = self.get_operation(operation_id)
        if previous:
            if (
                tuple(
                    previous[k]
                    for k in (
                        "source_resource",
                        "source_version",
                        "target_resource",
                        "transform",
                    )
                )
                != fields[1:]
            ):
                raise StateError("STATE_OPERATION_CHANGED")
            # Never replace the original pre-write observation on resume.
            return
        try:
            self.db.execute(
                "INSERT INTO operations(operation_id,source_resource,source_version,target_resource,transform,before_versions,status) VALUES(?,?,?,?,?,?,'intent')",
                (*fields, before),
            )
        except sqlite3.IntegrityError:
            raise StateError("STATE_OPERATION_CONFLICT") from None

    def complete(
        self, operation_id: str, target_version: str, verified: bool = False
    ) -> None:
        self._writing()
        operation_id, target_version = _text(operation_id), _text(target_version)
        if type(verified) is not bool:
            raise StateError("STATE_METADATA_INVALID")
        old = self.get_operation(operation_id)
        if old is None:
            raise StateError("STATE_INTENT_MISSING")
        if old["status"] == "complete" and old["target_version"] != target_version:
            raise StateError("STATE_MAPPING_CHANGED")
        self.db.execute(
            "UPDATE operations SET status='complete',target_version=?,verified=?,reason_code=NULL WHERE operation_id=?",
            (target_version, int(verified or old["verified"]), operation_id),
        )

    def invalidate_verification(self, operation_id: str) -> None:
        """Retain provenance mapping while revoking a stale verification result."""
        self._writing()
        operation_id = _text(operation_id)
        if self.get_operation(operation_id) is None:
            raise StateError("STATE_INTENT_MISSING")
        self.db.execute(
            "UPDATE operations SET verified=0 WHERE operation_id=?", (operation_id,)
        )

    def unknown(self, operation_id: str, reason_code: str) -> None:
        self._writing()
        operation_id, reason_code = _text(operation_id), _code(reason_code)
        row = self.get_operation(operation_id)
        if row is None:
            raise StateError("STATE_INTENT_MISSING")
        if row["status"] == "complete":
            raise StateError("STATE_OPERATION_COMPLETE")
        self.db.execute(
            "UPDATE operations SET status='unknown',reason_code=? WHERE operation_id=?",
            (reason_code, operation_id),
        )

    def confirm_scheduled(self, operation_id: str) -> None:
        """Clear ambiguity only after a lifecycle adapter confirms remote blocking.

        This is deliberately unavailable to value writes and rotation provenance.
        The caller must have just observed is_blocked(resource, version) == True.
        """
        self._writing()
        operation_id = _text(operation_id)
        operation = self.get_operation(operation_id)
        if operation is None:
            raise StateError("STATE_INTENT_MISSING")
        if not operation["transform"].startswith("delete-at:"):
            raise StateError("STATE_OPERATION_CONFLICT")
        if operation["status"] == "complete":
            raise StateError("STATE_OPERATION_COMPLETE")
        self.db.execute(
            "UPDATE operations SET status='intent',reason_code=NULL WHERE operation_id=?",
            (operation_id,),
        )

    def get_operation(self, operation_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM operations WHERE operation_id=?", (_text(operation_id),)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["before_versions"] = tuple(json.loads(result["before_versions"]))
        result["verified"] = bool(result["verified"])
        return result

    def list_operations(self) -> list[dict]:
        return [
            self.get_operation(row[0])
            for row in self.db.execute(
                "SELECT operation_id FROM operations ORDER BY operation_id"
            )
        ]

    def mapping(
        self, source_resource: str, source_version: str, transform: str = "identity-v1"
    ) -> dict | None:
        row = self.db.execute(
            "SELECT operation_id FROM operations WHERE source_resource=? AND source_version=? AND transform=? AND status='complete'",
            tuple(_text(x) for x in (source_resource, source_version, transform)),
        ).fetchone()
        return self.get_operation(row[0]) if row else None

    def set_stage(self, stage: str) -> None:
        self._writing()
        if stage not in {
            "planned",
            "prepared",
            "copied",
            "verified",
            "final_synced",
            "switched",
            "completed",
            "blocked",
            "needs_recovery",
        }:
            raise StateError("STATE_STAGE_INVALID")
        if (
            stage in {"copied", "verified", "final_synced", "switched", "completed"}
            and self.db.execute(
                "SELECT 1 FROM operations WHERE status!='complete' LIMIT 1"
            ).fetchone()
        ):
            raise StateError("STATE_UNRESOLVED_OPERATIONS")
        row = self.db.execute(
            "SELECT stage,last_stable_stage FROM job WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise StateError("STATE_NOT_BOUND")
        stable = (
            "planned",
            "prepared",
            "copied",
            "verified",
            "final_synced",
            "switched",
            "completed",
        )
        if stage in stable:
            if stable.index(stage) < stable.index(row["last_stable_stage"]):
                raise StateError("STATE_STAGE_REGRESSION")
            self.db.execute(
                "UPDATE job SET stage=?,last_stable_stage=? WHERE singleton=1",
                (stage, stage),
            )
        else:
            if row["stage"] == "completed" and stage != "needs_recovery":
                raise StateError("STATE_STAGE_REGRESSION")
            self.db.execute("UPDATE job SET stage=? WHERE singleton=1", (stage,))

    def status(self) -> dict:
        row = self.db.execute("SELECT * FROM job WHERE singleton=1").fetchone()
        counts = {"intent": 0, "unknown": 0, "complete": 0}
        counts.update(
            {
                row[0]: row[1]
                for row in self.db.execute(
                    "SELECT status,COUNT(*) FROM operations GROUP BY status"
                )
            }
        )
        installation = self.db.execute(
            "SELECT state,checked_at FROM installation_checks WHERE singleton=1"
        ).fetchone()
        return {
            "installation": dict(installation) if installation else None,
            "job": dict(row) if row else None,
            "operations": counts,
            "verified": self.db.execute(
                "SELECT COUNT(*) FROM operations WHERE verified=1 AND substr(source_version,1,2)!='__'"
            ).fetchone()[0],
        }
