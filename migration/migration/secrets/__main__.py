"""Independent AWS→OpenBao commands; no cloud or workload writes in read-only commands."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .common import SecretsError, digest, load, require, save, validate_config
from .models import MigrationError
from .state import State, StateError


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, '{"error":"INVALID_ARGUMENTS"}\n')


def parser():
    p = Parser(description="AWS Secrets Manager → 온프레미스 OpenBao 비밀값 이관")
    sub = p.add_subparsers(dest="command", required=True)
    for name in (
        "validate",
        "inventory",
        "mapping-template",
        "plan",
        "preflight",
        "apply",
        "resume",
        "verify",
        "status",
        "report",
        "access-report",
        "rotate-postgres",
        "check-postgres",
    ):
        q = sub.add_parser(name)
        q.add_argument("--config", required=True)
        q.add_argument("--out")
        if name == "plan":
            q.add_argument("--inventory", required=True)
            q.add_argument("--mapping", required=True)
        if name == "mapping-template":
            q.add_argument("--inventory", required=True)
        if name in {
            "preflight",
            "apply",
            "resume",
            "verify",
            "access-report",
            "rotate-postgres",
            "check-postgres",
        }:
            q.add_argument("--plan", required=True)
        if name in {"apply", "resume", "verify", "rotate-postgres", "check-postgres"}:
            q.add_argument("--preflight", required=True)
            q.add_argument("--state", required=True)
        if name in {"apply", "resume", "rotate-postgres"}:
            q.add_argument("--confirm-plan", required=True)
        if name in {"status", "report"}:
            q.add_argument("--state", required=True)
        if name == "access-report":
            q.add_argument("--identity-application", required=True)
            q.add_argument("--identity-verification", required=True)
            q.add_argument("--identity-checks", required=True)
        if name in {"rotate-postgres", "check-postgres"}:
            q.add_argument("--source-arn", required=True)
            q.add_argument("--spec", required=True)
            q.add_argument("--operation-id", required=True)
        if name == "rotate-postgres":
            q.add_argument("--readiness", required=True)
    return p


def execute(args):
    require(
        args.out
        or args.command not in {"inventory", "mapping-template", "plan", "preflight"},
        "PRIVATE_OUTPUT_FILE_REQUIRED",
    )
    config = validate_config(load(args.config))
    if args.out:
        inputs = [
            args.config,
            *config["preparation"].values(),
            *(config.get("identity") or {}).values(),
            config["target"]["ca_file"],
        ]
        inputs.extend(
            getattr(args, key, None)
            for key in (
                "inventory",
                "mapping",
                "plan",
                "preflight",
                "state",
                "spec",
                "readiness",
                "identity_application",
                "identity_verification",
                "identity_checks",
            )
        )
        if config["target"]["token_ref"].startswith("file:"):
            inputs.append(config["target"]["token_ref"][5:])
        require(
            all(Path(args.out).resolve() != Path(p).resolve() for p in inputs if p),
            "OUTPUT_OVERWRITES_INPUT",
        )
    if args.command == "validate":
        return {"valid": True, "scope": "configuration_only"}
    if args.command in {"status", "report"}:
        from .engine import report

        require(Path(args.state).is_file(), "STATE_NOT_FOUND")
        with State(args.state) as state:
            require(
                state.status()["job"] is None
                or state.status()["job"]["run_id"] == digest(config),
                "STATE_CONFIG_MISMATCH",
            )
            return report(state)
    if args.command == "plan":
        from .plan import build_plan

        return build_plan(config, load(args.inventory), load(args.mapping))
    if args.command == "mapping-template":
        from .common import check_artifact

        inventory = load(args.inventory)
        check_artifact(inventory, "secret_inventory", "inventory_hash")
        require(
            inventory["source"]
            == {k: config["source"][k] for k in ("account_id", "region")},
            "INVENTORY_SOURCE_MISMATCH",
        )
        return {
            "schema_version": 1,
            "inventory_hash": inventory["inventory_hash"],
            "bindings": [
                {
                    "source_arn": r["arn"],
                    "target_path": config["target"]["prefix"]
                    + "/"
                    + config["source"]["account_id"]
                    + "/"
                    + config["source"]["region"]
                    + "/"
                    + digest(r["arn"])[:24],
                    "business": None,
                    "environment": None,
                    "identity_assignments": [],
                    "access_mode": "identity" if config.get("identity") else "deferred",
                    "review_ref": None,
                }
                for r in inventory["resources"]
            ],
        }
    if args.command == "access-report":
        from .access import access_report

        return access_report(
            config,
            load(args.plan),
            load(args.identity_application),
            load(args.identity_verification),
            load(args.identity_checks),
        )
    from .source import AwsSource

    if args.command == "inventory":
        return AwsSource(config["source"]).inventory()
    from .target import OpenBao

    target = OpenBao(config["target"])
    plan = load(args.plan)
    if args.command == "preflight":
        from .plan import preflight

        return preflight(config, plan, AwsSource(config["source"]), target)
    if args.command in {"apply", "resume", "rotate-postgres"}:
        require(args.confirm_plan == plan["plan_hash"], "CONFIRMED_PLAN_MISMATCH")
    prepared = load(args.preflight)
    if args.command in {"rotate-postgres", "check-postgres"}:
        from .rotation import rotate

        require(
            re.fullmatch(r"[A-Za-z0-9_-]{8,100}", args.operation_id),
            "ROTATION_ID_INVALID",
        )
        rows = [r for r in plan["rows"] if r["source"]["arn"] == args.source_arn]
        require(
            len(rows) == 1 and Path(args.state).is_file(), "COPIED_RESOURCE_REQUIRED"
        )
        row = rows[0]
        rotation_path = str(
            Path(args.state).with_suffix(
                "." + row["binding_id"][:24] + ".rotation.sqlite3"
            )
        )
        if args.out:
            require(
                Path(args.out).resolve() != Path(rotation_path).resolve(),
                "OUTPUT_OVERWRITES_STATE",
            )
        rotation_spec = load(args.spec)
        admin_ref = rotation_spec.get("postgres", {}).get("admin_password_ref", "")
        if args.out and admin_ref.startswith("file:"):
            require(
                Path(args.out).resolve() != Path(admin_ref[5:]).resolve(),
                "OUTPUT_OVERWRITES_CREDENTIAL",
            )
        if args.command == "check-postgres":
            require(Path(rotation_path).is_file(), "ROTATION_STATE_NOT_FOUND")
        with State(args.state) as copied, State(rotation_path) as state:
            return rotate(
                config,
                plan,
                prepared,
                row,
                rotation_spec,
                load(args.readiness) if args.command == "rotate-postgres" else None,
                args.operation_id,
                target,
                copied,
                state,
                check_only=args.command == "check-postgres",
            )
    from .engine import run

    if args.command == "verify":
        require(Path(args.state).is_file(), "STATE_NOT_FOUND")
    with State(args.state) as state:
        return run(
            config,
            plan,
            prepared,
            AwsSource(config["source"]),
            target,
            state,
            verify_only=args.command == "verify",
        )


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = execute(args)
        if args.out:
            save(args.out, result)
            # Inventory and policy documents are private artifacts, not ordinary stdout.
            print(
                json.dumps(
                    {
                        "out": str(Path(args.out).absolute()),
                        "status": result.get("status", "written"),
                        "kind": result.get("kind"),
                    }
                )
            )
        else:
            print(json.dumps(result))
        return (
            3
            if result.get("status") in {"blocked", "partial"}
            or result.get("access_status") == "incomplete"
            else 0
        )
    except (SecretsError, MigrationError) as error:
        print(json.dumps({"error": error.code}))
        return 2
    except StateError:
        print('{"error":"STATE_CONFLICT_OR_LOCKED"}')
        return 2
    except Exception:
        print('{"error":"OPERATION_FAILED"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
