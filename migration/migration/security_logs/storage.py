"""Durable archive, filesystem limits and replay journal."""

import fcntl
import hashlib
import json
import os
import pwd
import sqlite3
import stat
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, datetime

from ..wazuh_infra.archive import validate_manifest
from .config import scope
from .errors import MigrationError, StorageBlocked

CHUNK = 1024 * 1024
INODE_RESERVE = 10000
FILE_OVERHEAD = 8192
METADATA_HEADROOM = 1024 * 1024
STAGE_NAME = ".security-logs-stage"

SCHEMA = """
CREATE TABLE IF NOT EXISTS binding (value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scans (
    source TEXT PRIMARY KEY,
    completed_end INTEGER,
    active_start INTEGER,
    active_end INTEGER
);
CREATE TABLE IF NOT EXISTS s3_issues (
    source TEXT NOT NULL,
    item TEXT NOT NULL,
    object_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (source, item)
);
CREATE TABLE IF NOT EXISTS archives (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    record TEXT NOT NULL,
    committed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS archives_source ON archives(source, committed);
CREATE TABLE IF NOT EXISTS items (
    source TEXT NOT NULL,
    id TEXT NOT NULL,
    archive_id TEXT NOT NULL REFERENCES archives(id) ON DELETE CASCADE,
    PRIMARY KEY (source, id)
);
"""


def now_ms():
    return int(datetime.now(UTC).timestamp() * 1000)


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def identity(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def owned_directory(path):
    if path.resolve(strict=True) != path:
        raise StorageBlocked("UNSAFE_DESTINATION")
    info = path.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise StorageBlocked("UNSAFE_DESTINATION")
    return info


def assert_writer():
    try:
        writer_uid = pwd.getpwnam("oneport-archive").pw_uid
    except KeyError:
        raise StorageBlocked("ARCHIVE_WRITER_UNAVAILABLE") from None
    if not writer_uid or writer_uid != os.geteuid():
        raise StorageBlocked("ARCHIVE_WRITER_REQUIRED")


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def open_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    stream = os.fdopen(fd, "rb")
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        stream.close()
        raise StorageBlocked("UNSAFE_ARCHIVE_FILE")
    return stream


def inspect_file(path):
    with open_private(path) as stream:
        size = os.fstat(stream.fileno()).st_size
        return size, hashlib.file_digest(stream, "sha256").hexdigest()


@contextmanager
def locked(config, write=False):
    with ExitStack() as stack:
        for path in sorted((config["root"], config["manifests"])):
            owned_directory(path)
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            try:
                mode = fcntl.LOCK_EX if write else fcntl.LOCK_SH
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StorageBlocked("TRANSFER_IN_PROGRESS") from None
        yield


def tree_bytes(root):
    total = root.stat().st_size
    for _, dirs, files, fd in os.fwalk(root):
        for name in dirs + files:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise StorageBlocked("UNSAFE_DESTINATION_CONTENTS")
            total += info.st_size
    return total


class Archive:
    def __init__(self, config):
        self.root = config["root"]
        self.manifests = config["manifests"]
        self.budget = config["budget_bytes"]
        self.reserve = config["reserve_bytes"]
        self.stage = self.root / STAGE_NAME
        self.manifest_stage = self.manifests / STAGE_NAME
        self.database = self.manifests / "security-logs.sqlite3"
        self.used = None
        self.db = None
        self.bound_locations = self.locations()
        self.binding = canonical(
            {
                "storage": self.bound_locations,
                "paths": [str(self.root), str(self.manifests)],
                "sources": {s["id"]: scope(s) for s in config["sources"]},
            }
        )

    def locations(self):
        return [
            (info.st_dev, info.st_ino)
            for info in map(owned_directory, (self.root, self.manifests))
        ]

    def check_location(self):
        if self.locations() != self.bound_locations:
            raise StorageBlocked("DESTINATION_CHANGED")

    def measure(self):
        self.check_location()
        self.used = tree_bytes(self.root)

    def capacity(self):
        if self.used is None:
            self.measure()
        self.check_location()
        raw = os.statvfs(self.root)
        journal = os.statvfs(self.manifests)
        raw_free = raw.f_bavail * raw.f_frsize
        journal_free = journal.f_bavail * journal.f_frsize
        return {
            "raw_used_bytes": self.used,
            "raw_budget_bytes": self.budget,
            "raw_available_bytes": raw_free,
            "manifest_available_bytes": journal_free,
            "reserve_bytes": self.reserve,
            "raw_writable_bytes": max(
                0, min(self.budget - self.used, raw_free - self.reserve)
            ),
            "inode_reserve_available": min(raw.f_favail, journal.f_favail)
            >= INODE_RESERVE,
        }

    def check_space(self, additional, *, logical=True):
        cap = self.capacity()
        if (
            (logical and self.used + additional + FILE_OVERHEAD > self.budget)
            or cap["raw_available_bytes"] - additional - METADATA_HEADROOM
            < self.reserve
            or cap["manifest_available_bytes"] < self.reserve + METADATA_HEADROOM
            or not cap["inode_reserve_available"]
        ):
            raise StorageBlocked("DESTINATION_CAPACITY_EXCEEDED")

    @contextmanager
    def connect(self, write=False):
        try:
            with open_private(self.database):
                pass
        except FileNotFoundError:
            if not write:
                yield False
                return
        if write:
            self.check_space(0, logical=False)
        uri = self.database.as_uri() + ("?mode=rwc" if write else "?mode=ro")
        with closing(sqlite3.connect(uri, uri=True)) as self.db:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys = ON")
            if write:
                self.db.execute("PRAGMA synchronous = FULL")
                self.db.executescript(SCHEMA)
            self.db.execute("BEGIN")
            with self.db:
                row = self.db.execute("SELECT value FROM binding").fetchone()
                if row is None and write:
                    self.db.execute("INSERT INTO binding VALUES (?)", (self.binding,))
                elif row is None or row["value"] != self.binding:
                    raise StorageBlocked("TRANSFER_SCOPE_OR_DESTINATION_CHANGED")
                if not write:
                    yield True
                    return
            if write:
                sync_directory(self.manifests)
                self.recover()
            yield True

    def archive_path(self, record):
        validate_manifest(record)
        path = self.root / record["local_relative_path"]
        if path.parent.resolve() != path.parent:
            raise StorageBlocked("UNSAFE_ARCHIVE_PATH")
        return path

    def manifest_path(self, key):
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise StorageBlocked("INVALID_ARCHIVE_STATE")
        return self.manifests / f"security-logs-{key}.json"

    def manifest_matches(self, key, record):
        with open_private(self.manifest_path(key)) as stream:
            return json.load(stream) == record

    def verify_file(self, key, record):
        return self.manifest_matches(key, record) and inspect_file(
            self.archive_path(record)
        ) == (record["bytes"], record["sha256"])

    def publish_manifest(self, key, record):
        try:
            if self.manifest_matches(key, record):
                return
            raise StorageBlocked("MANIFEST_CONFLICT")
        except FileNotFoundError:
            pass
        with self.manifest_stage.open("xb") as stream:
            stream.write(canonical(record).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(self.manifest_stage, self.manifest_path(key), follow_symlinks=False)
        sync_directory(self.manifests)
        self.manifest_stage.unlink()

    def recover(self):
        self.stage.unlink(missing_ok=True)
        self.manifest_stage.unlink(missing_ok=True)
        self.measure()
        for row in self.db.execute(
            "SELECT id, record FROM archives WHERE committed = 0"
        ).fetchall():
            key, record = row["id"], json.loads(row["record"])
            try:
                measured = inspect_file(self.archive_path(record))
            except FileNotFoundError:
                if self.manifest_path(key).exists():
                    raise StorageBlocked("ARCHIVE_RECOVERY_CONFLICT") from None
                with self.db:
                    self.db.execute("DELETE FROM archives WHERE id = ?", (key,))
                continue
            if measured != (record["bytes"], record["sha256"]):
                raise StorageBlocked("ARCHIVE_RECOVERY_CONFLICT")
            self.publish_manifest(key, record)
            with self.db:
                self.db.execute(
                    "UPDATE archives SET committed = 1 WHERE id = ?", (key,)
                )

    def seen(self, source, ids):
        placeholders = ",".join("?" for _ in ids)
        rows = self.db.execute(
            f"SELECT i.id FROM items i JOIN archives a ON a.id = i.archive_id "
            f"WHERE i.source = ? AND i.id IN ({placeholders}) AND a.committed = 1",
            (source, *ids),
        )
        return {row["id"] for row in rows}

    def write(
        self,
        source,
        ids,
        *,
        object_key,
        chunks,
        size,
        fidelity,
        version=None,
        remote_sha256=None,
    ):
        key = identity([source, ids])
        relative = f"security-logs/{source}/{key}.{'ndjson' if fidelity == 'api_records' else 'bin'}"
        digest = hashlib.sha256()
        count = 0
        self.check_space(size)
        try:
            with self.stage.open("xb") as stream:
                for chunk in chunks:
                    self.check_space(size - count)
                    count += len(chunk)
                    if count > size:
                        raise MigrationError("SOURCE_LENGTH_MISMATCH")
                    stream.write(chunk)
                    digest.update(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if count != size:
                raise MigrationError("SOURCE_LENGTH_MISMATCH")
            if remote_sha256 is not None and digest.hexdigest() != remote_sha256:
                raise MigrationError("SOURCE_CHECKSUM_MISMATCH")
            self.check_space(0)
            record = {
                "schema_version": 1,
                "source_id": source,
                "object_key": object_key,
                "source_version": version,
                "local_relative_path": relative,
                "bytes": count,
                "sha256": digest.hexdigest(),
                "archived_at": datetime.now(UTC).isoformat(),
                "verification": "remote_compared"
                if remote_sha256 is not None
                else "local_only",
                "archive_fidelity": fidelity,
            }
            path = self.archive_path(record)
            for directory in (path.parent.parent, path.parent):
                directory.mkdir(mode=0o700, exist_ok=True)
                owned_directory(directory)
                sync_directory(directory.parent)
            with self.db:
                self.db.execute(
                    "INSERT INTO archives(id, source, record) VALUES (?, ?, ?)",
                    (key, source, canonical(record)),
                )
                self.db.executemany(
                    "INSERT INTO items VALUES (?, ?, ?)",
                    ((source, item, key) for item in ids),
                )
            os.link(self.stage, path, follow_symlinks=False)
            sync_directory(path.parent)
            self.publish_manifest(key, record)
            with self.db:
                self.db.execute(
                    "UPDATE archives SET committed = 1 WHERE id = ?", (key,)
                )
            self.used += count + FILE_OVERHEAD
        finally:
            self.stage.unlink(missing_ok=True)
            self.manifest_stage.unlink(missing_ok=True)

    def s3_issue(self, source, item, object_key, reason):
        with self.db:
            self.db.execute(
                "INSERT INTO s3_issues VALUES (?, ?, ?, ?) ON CONFLICT(source, item) "
                "DO UPDATE SET reason = excluded.reason",
                (source, item, object_key, reason),
            )

    def clear_s3_issue(self, source, item):
        with self.db:
            self.db.execute(
                "DELETE FROM s3_issues WHERE source = ? AND item = ?", (source, item)
            )

    def s3_issues(self, source):
        rows = self.db.execute(
            "SELECT reason, COUNT(*) FROM s3_issues WHERE source = ? GROUP BY reason",
            (source,),
        )
        return dict(rows)

    def bounds(self, source, cutoff, lookback, full_rescan):
        scan = self.db.execute(
            "SELECT * FROM scans WHERE source = ?", (source,)
        ).fetchone()
        if scan and scan["active_end"] is not None:
            return scan["active_start"], scan["active_end"]
        previous = scan["completed_end"] if scan else None
        start = (
            max(0, previous - lookback)
            if previous is not None and not full_rescan
            else 0
        )
        if previous is not None and cutoff < previous:
            raise MigrationError("CLOCK_MOVED_BACKWARD")
        with self.db:
            self.db.execute(
                "INSERT INTO scans VALUES (?, NULL, ?, ?) ON CONFLICT(source) "
                "DO UPDATE SET active_start = excluded.active_start, active_end = excluded.active_end",
                (source, start, cutoff),
            )
        return start, cutoff

    def finish(self, source):
        with self.db:
            self.db.execute(
                "UPDATE scans SET completed_end = active_end, active_start = NULL, active_end = NULL "
                "WHERE source = ?",
                (source,),
            )

    def summary(self, source):
        row = self.db.execute(
            "SELECT COUNT(*) AS files, COALESCE(SUM(json_extract(record, '$.bytes')), 0) AS bytes "
            "FROM archives WHERE source = ? AND committed = 1",
            (source,),
        ).fetchone()
        scan = self.db.execute(
            "SELECT * FROM scans WHERE source = ?", (source,)
        ).fetchone()
        return {
            "archived_files": row["files"],
            "archived_bytes": row["bytes"],
            "archived_items": self.db.execute(
                "SELECT COUNT(*) FROM items i JOIN archives a ON a.id = i.archive_id "
                "WHERE i.source = ? AND a.committed = 1",
                (source,),
            ).fetchone()[0],
            "completed_through_ms": scan["completed_end"] if scan else None,
            "active_through_ms": scan["active_end"] if scan else None,
            "pending_files": self.db.execute(
                "SELECT COUNT(*) FROM archives WHERE source = ? AND committed = 0",
                (source,),
            ).fetchone()[0],
        }

    def records(self, source):
        rows = self.db.execute(
            "SELECT id, record FROM archives WHERE source = ? AND committed = 1 ORDER BY id",
            (source,),
        )
        for row in rows:
            yield row["id"], json.loads(row["record"])
