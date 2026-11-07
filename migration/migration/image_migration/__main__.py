from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from .config import MigrationError, load_config, require
from .plan import plan
from .registry import Registry, Skopeo
from .runner import Harbor, apply, journal, read_plan, verify, write_artifact
from .source import ECR


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Copy a private ECR registry to prepared Harbor."
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


def execute(args) -> dict:
    config = load_config(args.config)
    if args.operation in {"discover", "plan"}:
        source = ECR(config)
        inventory = source.inventory()
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
        harbor = Harbor(config)
        catalog = plan(
            config,
            inventory,
            Registry(config.source_host, source.authorization),
            harbor.registry,
        )
        plan_digest = write_artifact(args.output, catalog)
        items = [item for repo in catalog["repositories"] for item in repo["items"]]
        return {
            "status": "planned",
            "plan_digest": plan_digest,
            "plan_file": str(args.output),
            "repository_count": len(catalog["repositories"]),
            "image_count": len(items),
            "items_with_issues": sum(bool(item["issues"]) for item in items),
            "estimated_bytes": catalog["estimated_bytes"],
        }

    doc = read_plan(args.plan, args.plan_digest, config)
    if args.operation == "apply":
        require(config.exclusive_write, "EXCLUSIVE_DESTINATION_WRITE_REQUIRED")
    source = ECR(config)
    harbor = Harbor(config)
    with journal(config, args.plan_digest) as connection:
        if args.operation == "apply":
            copier = Skopeo(config)
            result = apply(
                config,
                doc,
                source,
                Registry(config.source_host, source.authorization),
                harbor,
                copier,
                connection,
            )
        else:
            result = verify(config, doc, source, harbor, connection)
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
