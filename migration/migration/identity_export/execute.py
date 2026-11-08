from __future__ import annotations

from ..identity.common import IdentityError, digest, now, require
from ..identity.execute import journal, ordered, record
from .plan import conflict_code, expected, validate_plan


def preflight(config, plan, targets):
    validate_plan(plan, config)
    targets.check_instance()
    result = {
        "schema_version": 1,
        "kind": "identity_export_preflight",
        "plan_hash": plan["plan_hash"],
        "config_hash": digest(config),
        "checked_at": now(),
        "snapshots": {},
        "blockers": [],
    }
    arns = set(targets.permission_sets())
    for r in ordered(plan["resources"]):
        if r["kind"] == "assignment" and r["payload"]["permission_set_arn"] not in arns:
            result["blockers"].append(
                {"resource_id": r["id"], "code": "PERMISSION_SET_NOT_FOUND"}
            )
        try:
            before = targets.read(r)
            result["snapshots"][r["id"]] = before
            if before is not None and before != expected(r):
                result["blockers"].append(
                    {"resource_id": r["id"], "code": conflict_code(r, before)}
                )
        except IdentityError as error:
            result["blockers"].append({"resource_id": r["id"], "code": error.code})
    result["status"] = "blocked" if result["blockers"] else "complete"
    result["preflight_hash"] = digest(result)
    return result


def check_preflight(config, plan, prepared):
    require(
        isinstance(prepared, dict)
        and prepared.get("kind") == "identity_export_preflight"
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


def apply(config, plan, prepared, directory, targets):
    validate_plan(plan, config)
    check_preflight(config, plan, prepared)
    result = {
        "schema_version": 1,
        "operation": "apply",
        "status": "complete",
        "plan_hash": plan["plan_hash"],
        "results": [],
        "migration_status": "pending_verification_and_identity_center_onboarding",
    }
    with journal(directory, plan["plan_hash"]) as db:
        for r in ordered(plan["resources"]):
            rid, want = r["id"], expected(r)
            entry = db.execute("SELECT * FROM operations WHERE id=?", (rid,)).fetchone()
            try:
                current = targets.read(r)
                if entry is not None:
                    if current == want:
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
                if current == want:
                    record(db, rid, "applied", False)
                    result["results"].append(
                        {"id": rid, "state": "unchanged", "created": False}
                    )
                    continue
                if current is not None:
                    raise IdentityError(conflict_code(r, current))
                record(db, rid, "applying", False)
                targets.write(r)
                require(targets.read(r) == want, "WRITE_NOT_CONFIRMED")
                record(db, rid, "applied", True)
                result["results"].append(
                    {"id": rid, "state": "applied", "created": True}
                )
            except IdentityError as error:
                row = db.execute(
                    "SELECT state FROM operations WHERE id=?", (rid,)
                ).fetchone()
                if row and row[0] == "applied":
                    state = "applied"
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
