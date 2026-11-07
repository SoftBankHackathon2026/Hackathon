"""Bound preflight snapshots, process lock and durable create/reconcile journal."""

from __future__ import annotations

import contextlib
import fcntl
import os
import sqlite3
from pathlib import Path

from .common import IdentityError, digest, load, now, require
from .plan import validate_plan


def ordered(resources):
    pending = {r["id"]: r for r in resources}
    done = set()
    while pending:
        ready = [r for r in pending.values() if set(r["depends_on"]) <= done]
        require(bool(ready), "INVALID_RESOURCE_DEPENDENCIES")
        for r in ready:
            yield r
            done.add(r["id"])
            del pending[r["id"]]


def check_infra(config):
    kc = config["targets"]["keycloak"]
    if "infra_result" not in kc:
        return "not_supplied_live_admin_preflight_required"
    result = load(kc["infra_result"])
    require(
        result.get("scope") == "infrastructure_only"
        and result.get("status") == "complete",
        "KEYCLOAK_INFRA_NOT_READY",
    )
    connection = result.get("connection") or {}
    require(
        connection.get("base_url", "").rstrip("/") == kc["base_url"].rstrip("/"),
        "KEYCLOAK_INFRA_ENDPOINT_MISMATCH",
    )
    require(
        not any(c.get("status") == "fail" for c in result.get("checks", [])),
        "KEYCLOAK_INFRA_CHECK_FAILED",
    )
    require(
        any(
            c.get("name") in {"signing_keys", "published_endpoint"}
            and c.get("status") == "pass"
            for c in result.get("checks", [])
        ),
        "KEYCLOAK_INFRA_CHECK_MISSING",
    )
    return "endpoint_checked_admin_access_verified_separately"


def preflight(config, plan, targets):
    validate_plan(plan, config)
    result = {
        "schema_version": 1,
        "kind": "identity_preflight",
        "plan_hash": plan["plan_hash"],
        "config_hash": digest(config),
        "checked_at": now(),
        "snapshots": {},
        "blockers": [],
        "infra": check_infra(config),
    }
    for r in ordered(plan["resources"]):
        try:
            before = targets.read(r)
            result["snapshots"][r["id"]] = before
            if before is not None and before != r["payload"]:
                result["blockers"].append(
                    {"resource_id": r["id"], "code": "EXISTING_RESOURCE_CONFLICT"}
                )
        except IdentityError as error:
            result["blockers"].append({"resource_id": r["id"], "code": error.code})
    result["status"] = "blocked" if result["blockers"] else "complete"
    result["preflight_hash"] = digest(result)
    return result


@contextlib.contextmanager
def journal(directory, plan_hash):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not directory.is_symlink(), "STATE_DIRECTORY_SYMLINK")
    os.chmod(directory, 0o700)
    lock_path = directory / "identity.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    db = None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IdentityError("MIGRATION_ALREADY_RUNNING") from None
        database = directory / "identity.sqlite3"
        require(not database.is_symlink(), "STATE_FILE_SYMLINK")
        # Create with restrictive permissions before sqlite opens it.
        touch = os.open(database, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(touch)
        db = sqlite3.connect(database)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, state TEXT NOT NULL, created INTEGER NOT NULL DEFAULT 0, reason TEXT, updated_at TEXT NOT NULL)"
        )
        current = db.execute(
            "SELECT value FROM metadata WHERE key='plan_hash'"
        ).fetchone()
        require(
            current is None or current[0] == plan_hash, "STATE_BELONGS_TO_ANOTHER_PLAN"
        )
        db.execute(
            "INSERT OR IGNORE INTO metadata VALUES ('plan_hash', ?)", (plan_hash,)
        )
        db.commit()
        yield db
    finally:
        if db is not None:
            db.close()
        os.close(fd)


def record(db, rid, state, created=False, reason=None):
    db.execute(
        "INSERT INTO operations VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET state=excluded.state, created=excluded.created, reason=excluded.reason, updated_at=excluded.updated_at",
        (rid, state, int(created), reason, now()),
    )
    db.commit()


def apply(config, plan, prepared, directory, targets):
    validate_plan(plan, config)
    require(
        prepared.get("kind") == "identity_preflight"
        and prepared.get("status") == "complete"
        and not prepared.get("blockers"),
        "PREFLIGHT_REQUIRED",
    )
    require(
        prepared.get("preflight_hash")
        == digest({k: v for k, v in prepared.items() if k != "preflight_hash"}),
        "PREFLIGHT_HASH_MISMATCH",
    )
    require(
        prepared.get("plan_hash") == plan["plan_hash"]
        and prepared.get("config_hash") == digest(config),
        "STALE_PREFLIGHT",
    )
    require(
        set(prepared["snapshots"]) == {r["id"] for r in plan["resources"]},
        "INCOMPLETE_PREFLIGHT",
    )
    result = {
        "schema_version": 1,
        "operation": "apply",
        "status": "complete",
        "plan_hash": plan["plan_hash"],
        "results": [],
        "migration_status": "pending_access_verification_and_handoffs",
    }
    with journal(directory, plan["plan_hash"]) as db:
        require(
            db.execute(
                "SELECT 1 FROM operations WHERE state='rolled_back' LIMIT 1"
            ).fetchone()
            is None,
            "ROLLED_BACK_PLAN_CANNOT_RESUME",
        )
        for r in ordered(plan["resources"]):
            rid = r["id"]
            entry = db.execute("SELECT * FROM operations WHERE id=?", (rid,)).fetchone()
            try:
                current = targets.read(r)
                if entry is not None:
                    require(
                        entry["state"] != "rolled_back",
                        "ROLLED_BACK_PLAN_CANNOT_RESUME",
                    )
                    if current == r["payload"]:
                        record(
                            db,
                            rid,
                            "applied",
                            bool(entry["created"]),
                            "RECONCILED" if entry["state"] != "applied" else None,
                        )
                        result["results"].append(
                            {
                                "id": rid,
                                "state": "applied",
                                "created": bool(entry["created"]),
                            }
                        )
                        continue
                    if entry["state"] == "applied":
                        raise IdentityError("APPLIED_RESOURCE_DRIFT")
                require(
                    current == prepared["snapshots"][rid],
                    "TARGET_CHANGED_SINCE_PREFLIGHT",
                )
                if current == r["payload"]:
                    record(db, rid, "applied", False)
                    result["results"].append(
                        {"id": rid, "state": "unchanged", "created": False}
                    )
                    continue
                require(current is None, "UPDATES_REQUIRE_NEW_REVIEW")
                record(db, rid, "applying", False)
                targets.write(r)
                require(targets.read(r) == r["payload"], "WRITE_NOT_CONFIRMED")
                record(db, rid, "applied", True)
                result["results"].append(
                    {"id": rid, "state": "applied", "created": True}
                )
            except IdentityError as error:
                # A timeout or lost response is not proof of failure. Resume always re-reads.
                row = db.execute(
                    "SELECT state FROM operations WHERE id=?", (rid,)
                ).fetchone()
                if row and row[0] in {"applied", "rolled_back"}:
                    state = row[0]
                else:
                    state = (
                        "unknown"
                        if row and row[0] in {"applying", "unknown"}
                        else "blocked"
                    )
                    record(db, rid, state, bool(entry and entry["created"]), error.code)
                result["results"].append(
                    {"id": rid, "state": state, "reason": error.code}
                )
                result["status"] = "partial"
                break
    return result


def status(directory):
    path = Path(directory) / "identity.sqlite3"
    require(path.is_file() and not path.is_symlink(), "STATE_NOT_FOUND")
    with sqlite3.connect("file:" + str(path.resolve()) + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        plan_hash = db.execute(
            "SELECT value FROM metadata WHERE key='plan_hash'"
        ).fetchone()[0]
        return {
            "schema_version": 1,
            "operation": "status",
            "status": "complete",
            "plan_hash": plan_hash,
            "operations": [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM operations ORDER BY updated_at, id"
                )
            ],
        }


def rollback_plan(config, plan, directory):
    validate_plan(plan, config)
    state = status(directory)
    require(state["plan_hash"] == plan["plan_hash"], "STATE_BELONGS_TO_ANOTHER_PLAN")
    entries = {r["id"]: r for r in state["operations"]}
    revocable = {
        "kc_membership",
        "kc_group_role",
        "k8s_binding",
        "bao_role",
        "bao_policy",
    }
    revoke, retain = [], []
    for r in reversed(list(ordered(plan["resources"]))):
        entry = entries.get(r["id"])
        if entry and entry["created"] and entry["state"] != "rolled_back":
            (revoke if r["kind"] in revocable else retain).append(r["id"])
    return {
        "schema_version": 1,
        "operation": "rollback-plan",
        "status": "complete",
        "plan_hash": plan["plan_hash"],
        "revoke": revoke,
        "retain": retain,
        "limitations": [
            "Identities, realms, clients, groups, roles and auth mounts are retained.",
            "Reconciled writes with uncertain creation ownership are retained.",
            "Existing user sessions are not globally revoked; remove newly-created Bao policies to withdraw their access.",
        ],
    }


def rollback(config, plan, directory, targets):
    result = rollback_plan(config, plan, directory)
    result["operation"] = "rollback"
    result["results"] = []
    lookup = {r["id"]: r for r in plan["resources"]}
    with journal(directory, plan["plan_hash"]) as db:
        for rid in result["revoke"]:
            r = lookup[rid]
            try:
                current = targets.read(r)
                require(
                    current is None or current == r["payload"],
                    "ROLLBACK_RESOURCE_DRIFT",
                )
                if current is not None:
                    targets.delete(r)
                require(targets.read(r) is None, "ROLLBACK_NOT_CONFIRMED")
                record(db, rid, "rolled_back", True)
                result["results"].append({"id": rid, "state": "rolled_back"})
            except IdentityError as error:
                result["status"] = "partial"
                result["results"].append(
                    {"id": rid, "state": "unknown", "reason": error.code}
                )
    return result
