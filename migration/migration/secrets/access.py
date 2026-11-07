"""Cross-check upstream access evidence, including original probe paths and subjects."""

from .common import require
from .plan import identity_context, validate_plan


def access_report(config, plan, application, verification, checks):
    validate_plan(config, plan)
    _, _, identity = identity_context(config)
    require(identity["plan_hash"] is not None, "IDENTITY_MAPPING_DEFERRED")
    for value, operation in ((application, "apply"), (verification, "verify")):
        require(
            value.get("schema_version") == 1
            and value.get("operation")
            in ({"apply", "resume"} if operation == "apply" else {"verify"})
            and value.get("plan_hash") == identity["plan_hash"],
            "IDENTITY_RESULT_MISMATCH",
        )
    require(application.get("status") == "complete", "IDENTITY_APPLY_INCOMPLETE")
    expected_resources = {
        r["id"] for r in identity["resources"] if r["kind"].startswith("bao_")
    }
    applied = {
        r["id"]
        for r in application.get("results", [])
        if r.get("state") in {"applied", "unchanged"}
    }
    configured = {
        r["id"]
        for r in verification.get("configuration", [])
        if r.get("status") == "pass"
    }
    require(
        expected_resources <= applied and expected_resources <= configured,
        "IDENTITY_OPENBAO_CONFIGURATION_UNVERIFIED",
    )
    require(
        checks.get("schema_version") == 1 and isinstance(checks.get("checks"), list),
        "IDENTITY_CHECKS_REQUIRED",
    )
    results = []
    for row in plan["rows"]:
        if not row["subjects"]:
            results.append(
                {"binding_id": row["binding_id"], "status": "identity_mapping_deferred"}
            )
            continue
        path = config["target"]["mount"] + "/data/" + row["target"]["path"]
        for subject in row["subjects"]:
            group = subject["target_group"]
            covered = {}
            for item in verification.get("access", []):
                index = item.get("index")
                require(
                    type(index) is int and 0 <= index < len(checks["checks"]),
                    "IDENTITY_PROBE_INDEX",
                )
                check = checks["checks"][index]
                if (
                    item.get("kind") != "openbao"
                    or item.get("group") != group
                    or item.get("status") != "pass"
                ):
                    continue
                require(
                    check.get("kind") == "openbao"
                    and check.get("group") == group
                    and type(check.get("allowed")) is bool
                    and check["allowed"] == item.get("expected_allowed")
                    and check.get("expected_subject"),
                    "IDENTITY_PROBE_MISMATCH",
                )
                require(
                    any(
                        r["kind"] == "bao_role"
                        and r["name"].split("/", 1)[1] == check.get("role")
                        and r["payload"]["bound_claims"]["groups"] == group
                        for r in identity["resources"]
                    ),
                    "IDENTITY_PROBE_ROLE_MISMATCH",
                )
                statuses = covered.setdefault(check["expected_subject"], set())
                # Upstream reports omit the original path: bind the check input explicitly.
                if check["allowed"] is True and check.get("path") == path:
                    statuses.add(True)
                elif check["allowed"] is False and check.get("path") != path:
                    statuses.add(False)
            results.append(
                {
                    "binding_id": row["binding_id"],
                    "assignment_id": subject["assignment_id"],
                    "status": "verified"
                    if any(states == {True, False} for states in covered.values())
                    else "missing_allow_or_deny_probe",
                }
            )
    return {
        "schema_version": 1,
        "kind": "secret_access_report",
        "plan_hash": plan["plan_hash"],
        "identity_plan_hash": identity["plan_hash"],
        "access_status": "verified"
        if results and all(r["status"] == "verified" for r in results)
        else "incomplete",
        "results": results,
        "verification_scope": "supplied_historical_identity_tool_evidence",
        "migration_complete": False,
        "application_status": "external_handoff_required",
    }
