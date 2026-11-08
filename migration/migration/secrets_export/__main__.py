from __future__ import annotations

import argparse
import json
from pathlib import Path

from migration.secrets.common import SecretsError, digest, load, require, save
from migration.secrets.models import MigrationError
from migration.secrets.state import State, StateError

from .common import validate_config
from .source import OpenBaoSource
from .target import SecretsManagerTarget

COMMANDS = ("validate", "plan", "apply", "resume", "verify", "status", "report")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, '{"error":"INVALID_ARGUMENTS"}\n')


def parser():
    p = Parser(description="온프레미스 OpenBao KV v2 → AWS Secrets Manager 비밀값 이관")
    sub = p.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        q = sub.add_parser(name)
        q.add_argument("--config", required=True)
        q.add_argument("--out")
        if name in {"apply", "resume", "verify"}:
            q.add_argument("--plan", required=True)
        if name in {"apply", "resume", "verify", "status", "report"}:
            q.add_argument("--state", required=True)
        if name in {"apply", "resume"}:
            q.add_argument("--confirm-plan", required=True)
    return p


def protected_inputs(args, config):
    return [
        args.config,
        config["source"]["ca_file"],
        config["source"]["token_ref"][5:],
        config["hash_key_ref"][5:],
        getattr(args, "plan", None),
        getattr(args, "state", None),
    ]


def execute(args):
    require(args.out or args.command != "plan", "PRIVATE_OUTPUT_FILE_REQUIRED")
    config = validate_config(load(args.config))
    if args.out:
        require(
            all(
                Path(args.out).resolve() != Path(p).resolve()
                for p in protected_inputs(args, config)
                if p
            ),
            "OUTPUT_OVERWRITES_INPUT",
        )
    if args.command == "validate":
        return {"valid": True, "scope": "configuration_only"}
    if args.command in {"status", "report"}:
        from .engine import report

        require(Path(args.state).is_file(), "STATE_NOT_FOUND")
        with State(args.state) as state:
            job = state.status()["job"]
            require(
                job is None or job["run_id"] == digest(config), "STATE_CONFIG_MISMATCH"
            )
            return report(state)
    source = OpenBaoSource(config["source"])
    target = SecretsManagerTarget(config["target"])
    if args.command == "plan":
        from .plan import build_plan

        return build_plan(config, source, target)
    from .engine import run

    plan = load(args.plan)
    if args.command in {"apply", "resume"}:
        require(args.confirm_plan == plan.get("plan_hash"), "CONFIRMED_PLAN_MISMATCH")
    else:
        require(Path(args.state).is_file(), "STATE_NOT_FOUND")
    with State(args.state) as state:
        return run(
            config,
            plan,
            source,
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
        return 3 if result.get("status") in {"blocked", "partial"} else 0
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
