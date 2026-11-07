"""PostgreSQL-only, durable-candidate rotation; no app mutation or password rollback."""

from __future__ import annotations

import copy
import secrets
from datetime import UTC, datetime

from .common import digest, json_value, keys, now, require
from .engine import lock_identity, operation, recheck
from .models import SecretValue
from .operations.postgres import PostgresSpec, PostgresTarget
from .target import SCHEMA, decode, encode


def pointer_parent(document, pointer):
    require(
        isinstance(pointer, str) and pointer.startswith("/") and len(pointer) <= 1024,
        "PASSWORD_POINTER_REQUIRED",
    )
    components = pointer[1:].split("/")
    parts = []
    for value in components:
        # JSON Pointer escaping is explicit; arrays and auto-created fields are excluded.
        i = 0
        while i < len(value):
            require(
                value[i] != "~" or (i + 1 < len(value) and value[i + 1] in "01"),
                "PASSWORD_POINTER_INVALID",
            )
            i += 2 if value[i] == "~" else 1
        parts.append(value.replace("~1", "/").replace("~0", "~"))
    parent = document
    for part in parts[:-1]:
        require(isinstance(parent, dict) and part in parent, "PASSWORD_FIELD_MISSING")
        parent = parent[part]
    require(
        isinstance(parent, dict)
        and parts[-1] in parent
        and isinstance(parent[parts[-1]], str),
        "PASSWORD_FIELD_MISSING",
    )
    return parent, parts[-1]


def password(value, spec):
    require(value.kind == "string", "POSTGRES_STRING_REQUIRED")
    if spec["format"] == "password_only":
        # Do not interpret structured credentials as a password-only value.
        try:
            document = json_value(value.data)
        except Exception:
            document = None
        require(not isinstance(document, (dict, list)), "JSON_PASSWORD_FIELD_REQUIRED")
        result = value
    else:
        document = json_value(value.data)
        parent, key = pointer_parent(document, spec["password_pointer"])
        result = SecretValue(parent[key].encode("utf-8"), "string")
    require(
        1 <= len(result.data) <= 4096 and b"\0" not in result.data, "PASSWORD_INVALID"
    )
    return result


def candidate(value, spec):
    replacement = secrets.token_urlsafe(48)
    if spec["format"] == "password_only":
        password(value, spec)
        return SecretValue(replacement.encode(), "string")
    document = json_value(value.data)
    parent, key = pointer_parent(document, spec["password_pointer"])
    parent[key] = replacement
    from .common import canonical

    return SecretValue(canonical(document).encode("utf-8"), "string")


def validate_spec(spec):
    keys(
        spec,
        {"product", "format", "password_pointer", "postgres"},
        {"product", "format", "postgres"},
    )
    require(spec["product"] == "postgresql", "ROTATION_PRODUCT_UNSUPPORTED")
    require(spec["format"] in {"password_only", "json"}, "ROTATION_FORMAT_UNSUPPORTED")
    if spec["format"] == "json":
        require("password_pointer" in spec, "PASSWORD_POINTER_REQUIRED")
    else:
        require("password_pointer" not in spec, "PASSWORD_POINTER_UNEXPECTED")
    keys(spec["postgres"], set(PostgresSpec.__dataclass_fields__))
    return PostgresSpec(**spec["postgres"])


def readiness(value, plan, row, operation_id):
    keys(
        value,
        {
            "schema_version",
            "plan_hash",
            "source_arn",
            "operation_id",
            "prepared_by",
            "issued_at",
            "expires_at",
            "application_prepared",
            "source_rotation_paused",
        },
        {
            "schema_version",
            "plan_hash",
            "source_arn",
            "operation_id",
            "prepared_by",
            "issued_at",
            "expires_at",
            "application_prepared",
            "source_rotation_paused",
        },
    )
    require(
        type(value["schema_version"]) is int
        and value["schema_version"] == 1
        and value["plan_hash"] == plan["plan_hash"]
        and value["source_arn"] == row["source"]["arn"]
        and value["operation_id"] == operation_id,
        "ROTATION_READINESS_SCOPE",
    )
    require(
        isinstance(value["prepared_by"], str)
        and value["prepared_by"]
        and value["application_prepared"] is True
        and value["source_rotation_paused"] is True,
        "ROTATION_EXTERNAL_PREPARATION_REQUIRED",
    )
    try:
        issued, expires = (
            datetime.fromisoformat(value[k].replace("Z", "+00:00"))
            for k in ("issued_at", "expires_at")
        )
        current = datetime.now(UTC)
        require(
            issued <= current < expires
            and 0 < (expires - issued).total_seconds() <= 1800,
            "ROTATION_READINESS_EXPIRED",
        )
    except (ValueError, TypeError, AttributeError):
        from .common import SecretsError

        raise SecretsError("ROTATION_READINESS_INVALID") from None


def rotate(
    config,
    plan,
    prepared,
    row,
    spec,
    ready,
    operation_id,
    target,
    copy_state,
    state,
    *,
    service=None,
    check_only=False,
):
    pg = validate_spec(spec)
    service = service or PostgresTarget(pg)
    opid = digest([row["binding_id"], operation_id, digest(spec)])
    name = row["target"]["path"]
    with state.lock(lock_identity(prepared)):
        recheck(config, plan, prepared, target)
        copied = copy_state.status()["job"]
        require(
            copied
            and copied["plan_digest"] == plan["plan_hash"]
            and copied["target_identity"] == lock_identity(prepared)
            and copied["stage"] == "verified",
            "VERIFIED_COPY_REQUIRED",
        )
        owner = copy_state.get_operation(operation(plan, row))
        require(
            owner and owner["status"] == "complete" and owner["verified"],
            "VERIFIED_RESOURCE_REQUIRED",
        )
        state.bind(
            digest([config, row["binding_id"], spec]),
            plan["plan_hash"],
            digest(row["source"]),
            lock_identity(prepared),
        )
        found = target.read_document(name)
        require(found is not None, "COPIED_VALUE_UNAVAILABLE")
        document, version = found
        require(
            document.get("schema") == SCHEMA
            and document.get("owner") == row["binding_id"]
            and document.get("copy_operation") == operation(plan, row)
            and document.get("source") == row["source"],
            "ROTATION_TARGET_OWNERSHIP",
        )
        rotation = document.get("rotation")
        existing = state.get_operation(opid)
        if existing and existing["status"] == "complete":
            require(
                rotation
                and rotation["operation_id"] == opid
                and rotation["phase"] == "complete"
                and document["current"] == opid,
                "ROTATION_CURRENT_CHANGED",
            )
            service.preflight()
            require(
                service.verify(password(decode(document["values"][opid]), spec))
                is True,
                "POSTGRES_RECONNECT_FAILED",
            )
            return result(operation_id, "verified")
        require(not check_only, "ROTATION_NOT_COMPLETE")
        readiness(ready, plan, row, operation_id)
        require(
            all(
                o["operation_id"] == opid or o["status"] == "complete"
                for o in state.list_operations()
            ),
            "OTHER_ROTATION_UNRESOLVED",
        )
        if rotation and rotation.get("phase") == "pending":
            require(
                rotation.get("operation_id") == opid and existing is not None,
                "OTHER_ROTATION_UNRESOLVED",
            )
        elif (
            rotation
            and rotation.get("operation_id") == opid
            and rotation.get("phase") == "complete"
        ):
            # Final target write succeeded but local acknowledgement was lost.
            service.preflight()
            require(
                existing is not None
                and document["current"] == opid
                and service.verify(password(decode(document["values"][opid]), spec))
                is True,
                "ROTATION_RECONCILIATION_FAILED",
            )
            state.complete(opid, str(version), verified=True)
            return result(operation_id, "verified")
        else:
            require(existing is None, "ROTATION_WRITE_UNKNOWN")
            original = decode(document["values"][document["current"]])
            password(original, spec)
            # Bind/check the actual server and ordinary account BEFORE preparing a candidate.
            service.preflight()
            require(
                service.verify(password(original, spec)) is True,
                "CURRENT_CREDENTIAL_NOT_FOR_ROLE",
            )
            new = candidate(original, spec)
            next_doc = copy.deepcopy(document)
            next_doc["values"][opid] = encode(new)
            next_doc["pending"] = opid
            next_doc["rotation"] = {
                "operation_id": opid,
                "spec_hash": digest(spec),
                "phase": "pending",
                "previous_current": document["current"],
            }
            state.intent(
                opid,
                row["source"]["arn"],
                "rotation-" + operation_id,
                name,
                transform=digest(spec),
            )
            recheck(config, plan, prepared, target)
            try:
                target.write_document(name, next_doc, version)
            except Exception:
                state.unknown(opid, "ROTATION_WRITE_UNKNOWN")
                raise
            document, version = next_doc, version + 1
        require(
            document["rotation"]["spec_hash"] == digest(spec)
            and document["pending"] == opid,
            "ROTATION_SPEC_CHANGED",
        )
        new = decode(document["values"][opid])
        readiness(ready, plan, row, operation_id)
        recheck(config, plan, prepared, target)
        try:
            require(
                target.read_document(name) == (document, version),
                "TARGET_CHANGED_DURING_ROTATION",
            )
            service.ensure(password(new, spec), opid)
            require(
                service.verify(password(new, spec)) is True, "POSTGRES_RECONNECT_FAILED"
            )
            require(
                target.read_document(name) == (document, version),
                "TARGET_CHANGED_DURING_ROTATION",
            )
            next_doc = copy.deepcopy(document)
            next_doc.update(previous=document["current"], current=opid, pending=None)
            next_doc["rotation"]["phase"] = "complete"
            recheck(config, plan, prepared, target)
            target.write_document(name, next_doc, version)
            confirmed = target.read_document(name)
            require(confirmed == (next_doc, version + 1), "ROTATION_WRITE_UNCONFIRMED")
            state.complete(opid, str(version + 1), verified=True)
        except Exception:
            state.unknown(opid, "ROTATION_EXTERNAL_UNCONFIRMED")
            raise
        return result(operation_id, "verified")


def result(operation_id, status):
    return {
        "schema_version": 1,
        "kind": "secret_rotation_report",
        "operation_id": operation_id,
        "rotation_status": status,
        "checked_at": now(),
        "application_status": "externally_prepared_not_verified_by_this_tool",
        "migration_complete": False,
        "source_secret_updated": False,
        "credential_metadata_rewrite_performed": False,
        "automatic_rollback": False,
    }
