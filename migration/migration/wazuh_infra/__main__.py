"""A single secret-free JSON result on stdout, including errors."""

import argparse
import json
import os

import yaml

from .config import OPERATIONS, ConfigError, local_file, validate
from .runner import run


def main():
    parser = argparse.ArgumentParser(description="Wazuh 설치·보관공간·검색 기반 준비")
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    try:
        local_file(args.config, "config", private=True)
        with open(args.config) as stream:
            raw = yaml.safe_load(stream)
        result = run(validate(raw, args.operation), args.operation)
    except ConfigError as exc:
        result = {
            "schema_version": 1,
            "operation": args.operation,
            "status": "blocked",
            "reason_codes": ["INPUT_INVALID"],
            "message": str(exc),
            "full_migration_verified": False,
        }
    except (OSError, ValueError, yaml.YAMLError):
        result = {
            "schema_version": 1,
            "operation": args.operation,
            "status": "failed",
            "reason_codes": ["INPUT_OR_EXECUTION_FAILED"],
            "full_migration_verified": False,
        }
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["status"] == "complete" else 3


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
