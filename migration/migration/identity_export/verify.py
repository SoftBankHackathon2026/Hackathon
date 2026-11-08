from __future__ import annotations

from ..identity.common import IdentityError
from .plan import expected, validate_plan


def verify(config, plan, targets):
    validate_plan(plan, config)
    targets.check_instance()
    result = {
        "schema_version": 1,
        "operation": "verify",
        "plan_hash": plan["plan_hash"],
        "configuration": [],
        "handoffs": plan["handoffs"],
        "pending_onboarding": [
            row["username"] for row in plan["user_rows"] if row["action"] == "create"
        ],
        "migration_status": "partial_pending_identity_center_onboarding",
    }
    for r in plan["resources"]:
        try:
            state = "pass" if targets.read(r) == expected(r) else "fail"
            result["configuration"].append({"id": r["id"], "status": state})
        except IdentityError as error:
            result["configuration"].append(
                {"id": r["id"], "status": "unknown", "reason": error.code}
            )
    failed = any(x["status"] != "pass" for x in result["configuration"])
    result["configuration_status"] = "failed" if failed else "complete"
    result["status"] = "failed" if failed else "partial"
    return result
