"""Platform-friendly independent CLI: stdout is a single non-secret JSON result."""

from __future__ import annotations

import argparse
import json

from .adapters import Targets
from .collect import collect
from .common import (
    IdentityError,
    load,
    private_text,
    private_write,
    require,
    validate_config,
)
from .execute import apply, preflight, rollback, rollback_plan, status
from .plan import create_plan, mapping_template, validate_plan
from .report import markdown
from .verify import verify


def parser():
    p = argparse.ArgumentParser(
        description="AWS Identity Center → 온프레미스 계정·권한 이관"
    )
    sub = p.add_subparsers(dest="operation", required=True)
    for name in (
        "collect",
        "mapping",
        "plan",
        "preflight",
        "apply",
        "resume",
        "verify",
        "status",
        "rollback-plan",
        "rollback",
        "export",
        "report",
    ):
        command = sub.add_parser(name)
        if name not in {"mapping", "status"}:
            command.add_argument("--config", required=True)
        if name in {"mapping", "plan"}:
            command.add_argument("--inventory", required=True)
        if name == "plan":
            command.add_argument("--mapping", required=True)
        if name in {
            "preflight",
            "apply",
            "resume",
            "verify",
            "rollback-plan",
            "rollback",
            "export",
            "report",
        }:
            command.add_argument("--plan", required=True)
        if name in {"apply", "resume"}:
            command.add_argument("--preflight", required=True)
        if name in {"apply", "resume", "rollback"}:
            command.add_argument(
                "--confirm-plan",
                required=True,
                help="검토한 plan_hash와 정확히 일치해야 합니다",
            )
        if name in {"apply", "resume", "status", "rollback-plan", "rollback"}:
            command.add_argument("--state-dir", required=True)
        if name == "verify":
            command.add_argument("--checks")
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
    if op == "plan":
        return create_plan(config, load(args.inventory), load(args.mapping))
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
            "mapping_rows": plan["mapping_rows"],
            "plan_hash": plan["plan_hash"],
        }
    if op == "rollback-plan":
        return rollback_plan(config, plan, args.state_dir)
    if op in {"apply", "resume", "rollback"}:
        require(args.confirm_plan == plan["plan_hash"], "CONFIRMED_PLAN_MISMATCH")
    targets = Targets(config, plan)
    if op == "preflight":
        return preflight(config, plan, targets)
    if op in {"apply", "resume"}:
        return apply(config, plan, load(args.preflight), args.state_dir, targets)
    if op == "rollback":
        return rollback(config, plan, args.state_dir, targets)
    return verify(config, plan, targets, load(args.checks) if args.checks else None)


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
    except Exception:  # noqa: BLE001 -- CLI is the final non-secret error boundary.
        # Neither third-party error text nor tracebacks may expose credentials.
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
    summary.setdefault("status", status_code)
    summary.setdefault("schema_version", 1)
    summary.setdefault("operation", args.operation)
    if args.out:
        summary["artifact_written"] = "reason_codes" not in result
    print(json.dumps(summary if args.out else result, ensure_ascii=False))
    return {
        "complete": 0,
        "planned": 0,
        "review_required": 3,
        "partial": 3,
        "blocked": 2,
        "failed": 1,
    }[status_code]


if __name__ == "__main__":
    raise SystemExit(main())
