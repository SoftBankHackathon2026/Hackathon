from __future__ import annotations

import argparse
import json
import sqlite3

from ..identity.common import IdentityError, load, private_text, private_write, require
from ..identity.execute import status
from .adapters import IdentityCenter
from .collect import collect
from .common import validate_config
from .execute import apply, preflight
from .plan import create_plan, mapping_template, validate_plan
from .report import markdown
from .verify import verify

UNEXPECTED_ERRORS = (
    OSError,
    sqlite3.Error,
    LookupError,
    TypeError,
    ValueError,
    AttributeError,
)

OPERATIONS = (
    "collect",
    "mapping",
    "plan",
    "preflight",
    "apply",
    "resume",
    "verify",
    "status",
    "export",
    "report",
)

EXIT_CODES = {
    "complete": 0,
    "planned": 0,
    "review_required": 3,
    "partial": 3,
    "blocked": 2,
    "failed": 1,
}


def parser():
    p = argparse.ArgumentParser(
        description="Keycloak → AWS IAM Identity Center 계정·권한 이관"
    )
    sub = p.add_subparsers(dest="operation", required=True)
    for name in OPERATIONS:
        command = sub.add_parser(name)
        if name not in {"mapping", "status"}:
            command.add_argument("--config", required=True)
        if name in {"mapping", "plan"}:
            command.add_argument("--inventory", required=True)
        if name == "plan":
            command.add_argument("--mapping", required=True)
        if name in {"preflight", "apply", "resume", "verify", "export", "report"}:
            command.add_argument("--plan", required=True)
        if name in {"apply", "resume"}:
            command.add_argument("--preflight", required=True)
            command.add_argument(
                "--confirm-plan",
                required=True,
                help="검토한 plan_hash와 정확히 일치해야 합니다",
            )
        if name in {"apply", "resume", "status"}:
            command.add_argument("--state-dir", required=True)
        if name == "report":
            command.add_argument("--state-dir")
            command.add_argument("--verification")
        command.add_argument(
            "--out",
            required=name
            in {"collect", "mapping", "plan", "preflight", "export", "report"},
        )
    return p


def run(args):
    op = args.operation
    config = validate_config(load(args.config)) if hasattr(args, "config") else None
    plan = load(args.plan) if hasattr(args, "plan") else None
    if plan is not None:
        validate_plan(plan, config, allow_blocked=op in {"report", "export"})
    if op == "collect":
        return collect(config)
    if op == "mapping":
        return mapping_template(load(args.inventory))
    if op == "status":
        return status(args.state_dir)
    if op == "report":
        state = status(args.state_dir) if args.state_dir else None
        verification = load(args.verification) if args.verification else None
        for artifact in (state, verification):
            if artifact is not None:
                require(
                    artifact.get("plan_hash") == plan["plan_hash"],
                    "REPORT_PLAN_MISMATCH",
                )
        require(
            verification is None or verification.get("operation") == "verify",
            "VERIFICATION_RESULT_REQUIRED",
        )
        return {
            "status": "complete",
            "operation": op,
            "plan_hash": plan["plan_hash"],
            "markdown": markdown(plan, state, verification),
        }
    if op == "export":
        return {
            "schema_version": 1,
            "operation": op,
            "status": "partial",
            "handoffs": plan["handoffs"],
            "exclusions": plan["exclusions"],
            "user_rows": plan["user_rows"],
            "mapping_rows": plan["mapping_rows"],
            "plan_hash": plan["plan_hash"],
        }
    if op in {"apply", "resume"}:
        require(args.confirm_plan == plan["plan_hash"], "CONFIRMED_PLAN_MISMATCH")
    targets = IdentityCenter(config)
    if op == "plan":
        return create_plan(config, load(args.inventory), load(args.mapping), targets)
    if op == "preflight":
        return preflight(config, plan, targets)
    if op in {"apply", "resume"}:
        return apply(config, plan, load(args.preflight), args.state_dir, targets)
    return verify(config, plan, targets)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args)
        if args.out:
            if args.operation == "report":
                private_text(args.out, result["markdown"])
            else:
                private_write(args.out, result)
    except IdentityError as error:
        result = {
            "schema_version": 1,
            "operation": args.operation,
            "status": error.status,
            "reason_codes": [error.code],
        }
    except UNEXPECTED_ERRORS:
        result = {
            "schema_version": 1,
            "operation": args.operation,
            "status": "failed",
            "reason_codes": ["CONTROLLER_EXECUTION_ERROR"],
        }
    summary = {
        k: result[k]
        for k in (
            "schema_version",
            "operation",
            "status",
            "reason_codes",
            "plan_hash",
            "inventory_hash",
            "migration_status",
        )
        if k in result
    }
    status_code = result.get(
        "status", "review_required" if args.operation == "mapping" else "failed"
    )
    summary["status"] = status_code
    summary["schema_version"] = 1
    summary["operation"] = args.operation
    if args.out:
        summary["artifact_written"] = "reason_codes" not in result
    print(json.dumps(summary if args.out else result, ensure_ascii=False))
    return EXIT_CODES[status_code]


if __name__ == "__main__":
    raise SystemExit(main())
