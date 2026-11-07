"""Explicit copy/verify/resume. State contains IDs and phases, never secret bytes."""

from __future__ import annotations

import hmac

from .common import SecretsError, check_artifact, digest, require
from .plan import validate_plan
from .target import SCHEMA, decode, encode


def operation(plan, row):
    return digest([plan["plan_hash"], row["binding_id"], "copy"])


def lock_identity(prepared):
    t = prepared["target"]
    return "openbao:" + t["cluster_id"] + "::" + t["accessor"]


def recheck(config, plan, prepared, target):
    validate_plan(config, plan)
    check_artifact(prepared, "secret_preflight", "preflight_hash")
    require(
        prepared["plan_hash"] == plan["plan_hash"]
        and prepared["status"] == "ready"
        and not prepared["conflicts"],
        "PREFLIGHT_REQUIRED",
    )
    observed = target.inspect([r["target"]["path"] for r in plan["rows"]])
    require(observed == prepared["target"], "TARGET_REPLACED")


def seed(plan, row, source):
    versions = row["versions"]
    values = {
        version: encode(source.read(row["source"]["arn"], version))
        for version in set(versions.values())
    }
    return {
        "schema": SCHEMA,
        "owner": row["binding_id"],
        "source": row["source"],
        "copy_operation": operation(plan, row),
        "values": values,
        "current": versions["AWSCURRENT"],
        "previous": versions.get("AWSPREVIOUS"),
        "pending": None,
        "rotation": None,
    }


def verify_seed(actual, expected):
    require(
        isinstance(actual, dict) and actual.get("schema") == SCHEMA,
        "TARGET_DOCUMENT_FORMAT",
    )
    for key in ("owner", "source", "copy_operation"):
        require(actual.get(key) == expected[key], "TARGET_OWNERSHIP_CONFLICT")
    for version, expected_value in expected["values"].items():
        require(version in actual.get("values", {}), "COPIED_VERSION_MISSING")
        observed, wanted = decode(actual["values"][version]), decode(expected_value)
        require(
            observed.kind == wanted.kind
            and hmac.compare_digest(observed.data, wanted.data),
            "VALUE_VERIFICATION_FAILED",
        )
    if actual.get("rotation") is None:
        require(
            all(
                actual.get(k) == expected[k] for k in ("current", "previous", "pending")
            ),
            "REFERENCE_CHANGED",
        )


def run(config, plan, prepared, source, target, state, *, verify_only=False):
    with state.lock(lock_identity(prepared)):
        try:
            return _run(
                config, plan, prepared, source, target, state, verify_only=verify_only
            )
        except Exception as error:
            expected_operational_handoff = (
                isinstance(error, SecretsError)
                and error.code == "ROTATION_ALREADY_STARTED_USE_ROTATION_CHECK"
            )
            job = state.status()["job"]
            same_job = (
                job
                and job["run_id"] == digest(config)
                and job["plan_digest"] == plan.get("plan_hash")
                and job["target_identity"] == lock_identity(prepared)
            )
            if same_job and not expected_operational_handoff:
                state.set_stage("needs_recovery")
            raise


def _run(config, plan, prepared, source, target, state, *, verify_only=False):
    recheck(config, plan, prepared, target)
    require(
        source.inventory()["source_fingerprint"] == plan["source_fingerprint"],
        "SOURCE_CHANGED_REPLAN_REQUIRED",
    )
    state.bind(
        digest(config),
        plan["plan_hash"],
        digest(plan["source"]),
        lock_identity(prepared),
    )
    # A copy journal is not an app transition journal. Rotations live in separate files.
    for row in plan["rows"]:
        path, op = row["target"]["path"], operation(plan, row)
        expected = seed(plan, row, source)
        # Recheck labels and target identity after secret reads and immediately before a write.
        require(
            source.inventory()["source_fingerprint"] == plan["source_fingerprint"],
            "SOURCE_CHANGED_REPLAN_REQUIRED",
        )
        recheck(config, plan, prepared, target)
        existing = target.read_document(path)
        record = state.get_operation(op)
        if existing is None:
            require(
                not verify_only and record is None,
                "COPIED_VALUE_UNAVAILABLE_OR_WRITE_UNKNOWN",
            )
            state.intent(op, row["source"]["arn"], "__copy__", path)
            try:
                target.write_document(path, expected, 0)
            except Exception:
                state.unknown(op, "WRITE_RESPONSE_UNKNOWN")
                raise
            existing = target.read_document(path)
            require(existing is not None, "WRITE_UNCONFIRMED")
        elif record is None and existing[0].get("copy_operation") != op:
            require(
                not verify_only
                and prepared.get("supersedes", {}).get(row["binding_id"]) == existing[1]
                and existing[0].get("owner") == row["binding_id"]
                and existing[0].get("rotation") is None
                and existing[0].get("pending") is None,
                "EXISTING_TARGET_NOT_OWNED_BY_JOURNAL",
            )
            state.intent(op, row["source"]["arn"], "__copy__", path)
            try:
                target.write_document(path, expected, existing[1])
            except Exception:
                state.unknown(op, "WRITE_RESPONSE_UNKNOWN")
                raise
            existing = target.read_document(path)
            require(existing is not None, "WRITE_UNCONFIRMED")
        else:
            require(record is not None, "EXISTING_TARGET_NOT_OWNED_BY_JOURNAL")
        verify_seed(existing[0], expected)
        require(
            existing[0].get("rotation") is None,
            "ROTATION_ALREADY_STARTED_USE_ROTATION_CHECK",
        )
        state.complete(op, str(existing[1]), verified=True)
    require(
        source.inventory()["source_fingerprint"] == plan["source_fingerprint"],
        "SOURCE_CHANGED_REPLAN_REQUIRED",
    )
    state.set_stage("verified")
    return report(state)


def report(state):
    status = state.status()
    job = status["job"]
    return {
        "schema_version": 1,
        "kind": "secret_report",
        "stage": job["stage"] if job else "unplanned",
        "copy_status": "verified"
        if job and job["stage"] == "verified"
        else "incomplete",
        "operation_counts": status["operations"],
        "value_and_version_payloads_in_report": False,
        "access_status": "requires_identity_mapping_and_access_verification",
        "rotation_status": "separate_operation_report",
        "application_status": "external_handoff_required",
        "migration_complete": False,
        "verification_scope": "historical_execution_record_not_fresh_environment_attestation",
    }
