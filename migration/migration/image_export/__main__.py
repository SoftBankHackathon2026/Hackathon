from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from migration.image_migration.config import MigrationError, require
from migration.image_migration.registry import Skopeo
from migration.image_migration.runner import journal, read_plan, verify, write_artifact

from .config import load_config
from .plan import plan
from .runner import apply
from .source import Harbor
from .target import Target


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Copy on-premises Harbor projects to a private Amazon ECR registry."
    )
    commands = root.add_subparsers(dest="operation", required=True)
    for operation in ("discover", "plan", "apply", "verify"):
        command = commands.add_parser(operation)
        command.add_argument("--config", type=Path, required=True)
        if operation in {"discover", "plan"}:
            command.add_argument(
                "--output", type=Path, required=True, help="New inventory or plan file"
            )
        else:
            command.add_argument("--plan", type=Path, required=True)
            command.add_argument("--plan-digest", required=True)
    return root


def execute(args, harbor=None, target=None, copier=None) -> dict:
    config = load_config(args.config)
    harbor = harbor or Harbor(config)
    if args.operation in {"discover", "plan"}:
        inventory = harbor.inventory()
        if args.operation == "discover":
            write_artifact(args.output, inventory)
            return {
                "status": "discovered",
                "repository_count": len(inventory["repositories"]),
                "image_count": sum(
                    len(repo["images"]) for repo in inventory["repositories"]
                ),
                "inventory_file": str(args.output),
            }
        target = target or Target(config)
        catalog = plan(config, inventory, harbor.registry, target)
        plan_digest = write_artifact(args.output, catalog)
        items = [item for repo in catalog["repositories"] for item in repo["items"]]
        return {
            "status": "planned",
            "plan_digest": plan_digest,
            "plan_file": str(args.output),
            "repository_count": len(catalog["repositories"]),
            "repositories_to_create": sum(
                not repo["target_exists"] for repo in catalog["repositories"]
            ),
            "image_count": len(items),
            "items_with_issues": sum(bool(item["issues"]) for item in items),
            "estimated_bytes": catalog["estimated_bytes"],
        }

    doc = read_plan(args.plan, args.plan_digest, config)
    if args.operation == "apply":
        require(config.exclusive_write, "EXCLUSIVE_DESTINATION_WRITE_REQUIRED")
    target = target or Target(config)
    with journal(config, args.plan_digest, "ecr") as connection:
        if args.operation == "apply":
            result = apply(
                config, doc, harbor, target, copier or Skopeo(config), connection
            )
        else:
            result = verify(config, doc, harbor, target, connection)
    return {**result, "binding": config.binding, "plan_digest": args.plan_digest}


def main() -> int:
    args = parser().parse_args()
    try:
        result = execute(args)
    except MigrationError as exc:
        result = {"status": "failed", "reason": exc.code}
    except (OSError, sqlite3.Error):
        result = {"status": "failed", "reason": "LOCAL_IO_FAILED"}
    except KeyboardInterrupt:
        result = {"status": "interrupted", "reason": "OPERATOR_INTERRUPTED"}
    print(
        json.dumps(
            {"schema_version": 1, "operation": args.operation, **result},
            ensure_ascii=False,
        )
    )
    return 3 if result["status"] in {"failed", "partial", "interrupted"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
