from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import time
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from .config import DIGEST, Config, HarborCredentials, MigrationError, matches, require
from .plan import drift, image_kind, now, validate_plan
from .registry import Registry, Verifier, child_descriptors, encoded_robot


def json_bytes(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()


def write_artifact(path: Path, value: dict) -> str:
    raw = json_bytes(value)
    temp = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temp = Path(file.name)
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        # An existing plan, credential file, or symlink is never replaced.
        os.link(temp, path)
    except FileExistsError:
        raise MigrationError("OUTPUT_ALREADY_EXISTS") from None
    except OSError:
        raise MigrationError("OUTPUT_WRITE_FAILED") from None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def read_plan(path: Path, expected: str, config: Config) -> dict:
    try:
        raw = path.read_bytes()
        require(
            matches(DIGEST, expected)
            and expected == "sha256:" + hashlib.sha256(raw).hexdigest(),
            "PLAN_DIGEST_MISMATCH",
        )
        doc = json.loads(raw)
    except (OSError, ValueError):
        raise MigrationError("PLAN_READ_FAILED") from None
    validate_plan(config, doc)
    return doc


class Harbor:
    def __init__(self, config: Config):
        self.config = config
        self.credentials = HarborCredentials(config)
        self.clients: dict[tuple[str, str], Registry] = {}

    def registry(self, project: str, role: str = "reader") -> Registry:
        key = (project, role)
        if key not in self.clients:
            self.credentials.account(project, role)
            self.clients[key] = Registry(
                self.config.target_host,
                lambda: encoded_robot(self.credentials.account(project, role)),
                self.config.ca_ref,
            )
        return self.clients[key]


@contextmanager
def journal(config: Config, plan_digest: str, lock_prefix: str = "harbor"):
    root = config.state_dir
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    meta = root.lstat()
    require(
        stat.S_ISDIR(meta.st_mode)
        and meta.st_uid == os.geteuid()
        and not meta.st_mode & 0o077,
        "UNSAFE_STATE_DIRECTORY",
    )
    host_key = hashlib.sha256(config.target_host.encode()).hexdigest()
    lock_path = root / f"{lock_prefix}-{host_key}.lock"
    state_path = root / f"{plan_digest.split(':', 1)[1]}.sqlite3"
    lock = os.open(lock_path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    with os.fdopen(lock, "r+b"):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MigrationError("DESTINATION_BUSY") from None
        descriptor = os.open(state_path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(state_path)) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS progress (
                       phase TEXT, reference TEXT, status TEXT, reason TEXT, updated_at TEXT,
                       PRIMARY KEY (phase, reference)
                   )"""
            )
            yield connection


def recorded(connection, phase: str, reference: str) -> str | None:
    row = connection.execute(
        "SELECT status FROM progress WHERE phase = ? AND reference = ?",
        (phase, reference),
    ).fetchone()
    return row[0] if row else None


def record(
    connection, phase: str, reference: str, status: str, reason: str | None
) -> None:
    with connection:
        connection.execute(
            "INSERT OR REPLACE INTO progress VALUES (?, ?, ?, ?, ?)",
            (phase, reference, status, reason, now()),
        )


def archive_dependencies(item: dict, active: dict, source: Registry, repo: str) -> None:
    """Validate an index's immutable dependency graph before handing it to Skopeo."""
    seen: set[str] = set()

    def visit(identity: str, depth: int) -> None:
        if identity in seen:
            return
        require(depth <= 32, "INDEX_DEPTH_LIMIT")
        seen.add(identity)
        child = active.get(identity)
        require(child is None or child["state"] == "ACTIVE", "SOURCE_RESTORE_PENDING")
        current = source.manifest(repo, identity)
        require(current is not None, "SOURCE_MANIFEST_MISSING")
        manifest = current[1]
        if depth == 0:
            require(
                image_kind(item, manifest) == item["kind"],
                "SOURCE_FORMAT_CHANGED_REPLAN",
            )
        if image_kind(item, manifest) == "index":
            children = {dependency for dependency, _ in child_descriptors(manifest)}
            if depth == 0:
                require(
                    children == set(item["children"]), "SOURCE_INDEX_REPLAN_REQUIRED"
                )
            for dependency in sorted(children):
                visit(dependency, depth + 1)

    visit(item["digest"], 0)


def copy_reference(config, ecr, copier, harbor, repo, identity, tag):
    target = repo["target"]
    project = target.split("/", 1)[0]
    # Use the writer for the immediate comparison and copy. The final verify uses the reader.
    writer = harbor.registry(project, "writer")
    for attempt in range(3):
        try:
            previous = writer.manifest(target, tag)
            if previous is not None:
                require(previous[0] == identity, "TARGET_TAG_CONFLICT")
                return "already_present" if attempt == 0 else "present_after_retry"
            copier.copy(
                f"{config.source_host}/{repo['source']}@{identity}",
                f"{config.target_host}/{target}:{tag}",
                ecr.authorization(refresh=attempt > 0),
                harbor.credentials.account(project, "writer"),
            )
            result = writer.manifest(target, tag)
            require(
                result is not None and result[0] == identity, "COPY_RESULT_UNCONFIRMED"
            )
            return "copied"
        except MigrationError as exc:
            if (
                exc.code
                not in {
                    "COPY_AUTH_FAILED",
                    "COPY_TEMPORARY_FAILURE",
                    "COPY_TIMED_OUT",
                    "COPY_RESULT_UNCONFIRMED",
                    "REGISTRY_CONNECTION_FAILED",
                    "REGISTRY_TEMPORARY_FAILURE",
                }
                or attempt == 2
            ):
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def outcome(repo: dict, item: dict, tag: str | None) -> dict:
    return {
        "source_repository": repo["source"],
        "source_tags": item["tags"],
        "source_digest": item["digest"],
        "target_repository": repo["target"],
        "target_tag": tag,
    }


def address(config: Config, repo: dict, item: dict, tag: str | None) -> str:
    suffix = f":{tag}" if tag is not None else "@" + item["digest"]
    return f"{config.target_host}/{repo['target']}{suffix}"


def apply(config, doc, ecr, source, harbor, copier, connection) -> dict:
    active = {
        repo["repository"]: {item["digest"]: item for item in repo["images"]}
        for repo in ecr.inventory()["repositories"]
    }
    results = []
    for repo in doc["repositories"]:
        current = active.get(repo["source"], {})
        items = sorted(
            repo["items"],
            key=lambda item: (
                not item["target_tags"],
                item["kind"] != "index",
                item["digest"],
            ),
        )
        for item in items:
            identity = item["digest"]
            kind = item["kind"]
            if kind == "attachment":
                results.append(
                    {**outcome(repo, item, None), "status": "excluded_attachment"}
                )
                continue
            dependency_error = None
            try:
                require(kind != "unsupported", "UNSUPPORTED_IMAGE_FORMAT")
                require(identity in current, "SOURCE_IMAGE_MISSING")
                require(
                    current[identity]["state"] == "ACTIVE", "SOURCE_RESTORE_PENDING"
                )
                if kind == "index" or not item["source_checked"]:
                    archive_dependencies(item, current, source, repo["source"])
            except MigrationError as exc:
                dependency_error = exc.code

            for tag in item["target_tags"] or [None]:
                reference = address(config, repo, item, tag)
                status, reason = "failed", None
                previous = recorded(connection, "apply", reference)
                try:
                    if dependency_error:
                        raise MigrationError(dependency_error)
                    if tag is None:
                        reader = harbor.registry(
                            repo["target"].split("/", 1)[0], "writer"
                        )
                        require(
                            reader.manifest(repo["target"], identity) is not None,
                            "TARGET_CHILD_MISSING",
                        )
                        status = "present_via_index"
                    else:
                        status = copy_reference(
                            config, ecr, copier, harbor, repo, identity, tag
                        )
                except MigrationError as exc:
                    reason = exc.code
                    if reason in {
                        "SOURCE_RESTORE_PENDING",
                        "TARGET_TAG_CONFLICT",
                        "SOURCE_IMAGE_MISSING",
                        "UNSUPPORTED_IMAGE_FORMAT",
                        "SOURCE_FORMAT_CHANGED_REPLAN",
                        "TARGET_CHILD_MISSING",
                        "SOURCE_INDEX_REPLAN_REQUIRED",
                    }:
                        status = "blocked"
                record(connection, "apply", reference, status, reason)
                results.append(
                    {
                        **outcome(repo, item, tag),
                        "target_address": reference,
                        "status": status,
                        "reason": reason,
                        "previous_status": previous,
                    }
                )
    return {
        "status": "partial"
        if any(row["status"] in {"failed", "blocked"} for row in results)
        else "copied",
        "items": results,
    }


def verify(config, doc, ecr, harbor, connection) -> dict:
    checks = []
    mappings = []
    verifiers: dict[str, Verifier] = {}
    for repo in doc["repositories"]:
        project = repo["target"].split("/", 1)[0]
        for item in repo["items"]:
            if item["kind"] == "attachment":
                checks.append(
                    {**outcome(repo, item, None), "status": "excluded_attachment"}
                )
                continue
            identity = item["digest"]
            for tag in item["target_tags"] or [None]:
                reference = address(config, repo, item, tag)
                row = {**outcome(repo, item, tag), "target_address": reference}
                status, reason = "verified", None
                try:
                    require(item["kind"] != "unsupported", "UNSUPPORTED_IMAGE_FORMAT")
                    if project not in verifiers:
                        verifiers[project] = Verifier(harbor.registry(project))
                    check = verifiers[project]
                    check.verify(repo["target"], tag or identity, identity)
                    if tag is not None:
                        check.verify(repo["target"], identity, identity)
                    suffix = f":{tag}" if tag in item["tags"] else "@" + identity
                    mappings.append(
                        {
                            **row,
                            "source_address": f"{config.source_host}/{repo['source']}{suffix}",
                            "target_digest_address": address(config, repo, item, None),
                            "synthetic_tag": tag is not None
                            and tag not in item["tags"],
                            "verified_at": now(),
                        }
                    )
                except MigrationError as exc:
                    status, reason = "failed", exc.code
                record(connection, "verify", reference, status, reason)
                checks.append({**row, "status": status, "reason": reason})

    inventory_error = None
    changes = None
    verified_mappings = []
    try:
        inventory = ecr.inventory()
        changes = drift(doc, inventory)
        current = {
            (repo["repository"], item["digest"]): item
            for repo in inventory["repositories"]
            for item in repo["images"]
        }
        for mapping in mappings:
            source = current.get(
                (mapping["source_repository"], mapping["source_digest"])
            )
            if source is not None and (
                mapping["target_tag"] not in mapping["source_tags"]
                or mapping["target_tag"] in source["tags"]
            ):
                verified_mappings.append(mapping)
        for row in checks:
            source = current.get((row["source_repository"], row["source_digest"]))
            if (
                row.get("reason") == "TARGET_MANIFEST_MISSING"
                and source is not None
                and source["state"] != "ACTIVE"
            ):
                row.update(status="blocked", reason="SOURCE_RESTORE_PENDING")
                record(
                    connection,
                    "verify",
                    row["target_address"],
                    row["status"],
                    row["reason"],
                )
    except MigrationError as exc:
        inventory_error = exc.code
    complete = (
        changes is not None
        and not any(changes.values())
        and all(row["status"] in {"verified", "excluded_attachment"} for row in checks)
    )
    return {
        "status": "verified" if complete else "partial",
        "items": checks,
        "verified_mappings": verified_mappings,
        "source_drift": changes,
        "source_inventory_error": inventory_error,
    }
