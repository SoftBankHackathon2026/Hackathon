from __future__ import annotations

from migration.secrets.common import digest, require

from .common import hash_key, same_hash, value_hash
from .plan import desired_tags, validate_plan

TRANSFORM = "kv2-json-to-secretsmanager-v1"


def operation(plan, row):
    return digest([plan["plan_hash"], row["binding_id"], "export"])


def source_identity(observed):
    return "openbao:" + observed["cluster_id"] + ":" + observed["accessor"]


def source_resource(row):
    return "openbao:" + row["source"]["mount"] + "/" + row["source"]["path"]


def run(config, plan, source, target, state, *, verify_only=False):
    key = hash_key(config)
    validate_plan(config, plan, key)
    with state.lock(plan["target"]["identity"]):
        try:
            return _run(config, plan, key, source, target, state, verify_only)
        except Exception:
            job = state.status()["job"]
            if (
                job
                and job["run_id"] == digest(config)
                and job["plan_digest"] == plan["plan_hash"]
            ):
                state.set_stage("needs_recovery")
            raise


def _run(config, plan, key, source, target, state, verify_only):
    observed = source.identity()
    require(observed == plan["source"]["observed"], "SOURCE_REPLACED")
    source.check_read_only([r["source"]["path"] for r in plan["rows"]])
    require(target.identity() == plan["target"]["identity"], "TARGET_IDENTITY_MISMATCH")
    state.bind(
        digest(config),
        plan["plan_hash"],
        source_identity(observed),
        plan["target"]["identity"],
    )
    for row in plan["rows"]:
        unchanged_source(row, key, source)
    for row in plan["rows"]:
        export_row(plan, row, key, source, target, state, verify_only)
    state.set_stage("verified")
    return report(state)


def write(plan, row, op, value, observed, target, state):
    require(target.identity() == plan["target"]["identity"], "TARGET_IDENTITY_MISMATCH")
    if row["action"] == "create":
        require(observed is None, "TARGET_CREATED_OUTSIDE_PLAN")
    else:
        require(
            observed is not None
            and observed["arn"] == row["target"]["arn"]
            and observed["current_version"] == row["target"]["version_id"],
            "TARGET_CHANGED_REPLAN_REQUIRED",
        )
    state.intent(
        op, source_resource(row), str(row["source"]["version"]), row["target"]["name"]
    )
    tags = desired_tags(plan, row)
    try:
        if row["action"] == "create":
            target.create(row["target"]["name"], value, op, tags)
        else:
            target.put(row["target"]["arn"], value, op)
            target.tag(row["target"]["arn"], tags)
    except Exception:
        state.unknown(op, "WRITE_RESPONSE_UNKNOWN")
        raise


def unchanged_source(row, key, source):
    value, version = source.read(row["source"]["path"])
    require(
        version == row["source"]["version"]
        and same_hash(value_hash(key, value), row["source_value_hash"]),
        "SOURCE_CHANGED_REPLAN_REQUIRED",
    )
    return value


def export_row(plan, row, key, source, target, state, verify_only):
    op = operation(plan, row)
    name, action = row["target"]["name"], row["action"]
    value = unchanged_source(row, key, source)
    expected = row["target"]["version_id"] if action == "unchanged" else op
    observed = target.describe(name)
    if observed is None or observed["current_version"] != expected:
        require(
            not verify_only and action != "unchanged",
            "TARGET_CHANGED_OR_NOT_EXPORTED",
        )
        write(plan, row, op, value, observed, target, state)
        observed = target.describe(name)
        require(
            observed is not None and observed["current_version"] == expected,
            "WRITE_UNCONFIRMED",
        )
    require(
        row["target"]["arn"] is None or observed["arn"] == row["target"]["arn"],
        "TARGET_REPLACED",
    )
    require(
        not observed["scheduled_deletion"]
        and not observed["pending"]
        and not observed["managed_by_service"],
        "TARGET_STATE_UNSAFE",
    )
    if action != "unchanged":
        tags = desired_tags(plan, row)
        if any(observed["tags"].get(k) != v for k, v in tags.items()):
            require(not verify_only, "TARGET_TAGS_MISMATCH")
            target.tag(observed["arn"], tags)
    version_id, current = target.current_value(observed["arn"])
    require(
        version_id == expected
        and same_hash(value_hash(key, current), row["source_value_hash"]),
        "VALUE_VERIFICATION_FAILED",
    )
    state.intent(op, source_resource(row), str(row["source"]["version"]), name)
    state.complete(op, version_id, verified=True)


def report(state):
    status = state.status()
    job = status["job"]
    return {
        "schema_version": 1,
        "kind": "secret_export_report",
        "stage": job["stage"] if job else "unplanned",
        "copy_status": "verified"
        if job and job["stage"] == "verified"
        else "incomplete",
        "operation_counts": status["operations"],
        "verified_count": status["verified"],
        "value_payloads_in_report": False,
        "rotation_status": "not_supported_by_this_tool",
        "application_status": "external_handoff_required",
        "migration_complete": False,
        "verification_scope": "historical_execution_record_not_fresh_environment_attestation",
    }
