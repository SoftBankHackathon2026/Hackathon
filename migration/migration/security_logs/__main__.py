"""Platform entry point. Stdout contains one result without log or secret contents."""

import argparse
import json
import os

import yaml

from ..wazuh_infra.config import ConfigError
from .config import load
from .errors import MigrationError
from .runner import run


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="AWS 보안로그 보관과 Wazuh 검색 대조")
    parser.add_argument("operation", choices=("plan", "run", "status", "verify"))
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--full-rescan",
        action="store_true",
        help="run의 새 CloudWatch 조회 범위를 처음부터 시작",
    )
    args = parser.parse_args()
    if args.full_rescan and args.operation != "run":
        parser.error("--full-rescan requires run")
    try:
        result = run(load(args.config), args.operation, args.full_rescan)
    except (
        ConfigError,
        yaml.YAMLError,
        ValueError,
        TypeError,
        AttributeError,
        OSError,
    ):
        result = {"status": "blocked", "reason_codes": ["INPUT_INVALID_OR_UNREADABLE"]}
    except MigrationError as error:
        result = {"status": "blocked", "reason_codes": [str(error)]}
    except KeyboardInterrupt:
        result = {
            "status": "partial",
            "reason_codes": ["INTERRUPTED_RETRY_SAME_CONFIG"],
        }
    print(
        json.dumps(
            {
                "schema_version": 1,
                "operation": args.operation,
                "archive_complete": False,
                "search_verified": False,
                "full_migration_verified": False,
                **result,
            },
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0 if result["status"] == "complete" else 3


if __name__ == "__main__":
    raise SystemExit(main())
