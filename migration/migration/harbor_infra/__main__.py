"""One safe JSON result per invocation; no raw provider/Ansible exceptions."""

import argparse
import json

from .config import OPERATIONS, ConfigError, load
from .runner import run


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Harbor infrastructure preparation (no workload image transfer)"
    )
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args.operation, load(args.config, args.operation))
    except ConfigError as exc:
        result = {
            "schema_version": 1,
            "operation": args.operation,
            "status": "blocked",
            "reason_codes": ["INVALID_CONFIGURATION"],
            "message": str(exc),
        }
    except KeyboardInterrupt:
        result = {
            "schema_version": 1,
            "operation": args.operation,
            "status": "failed",
            "reason_codes": ["INTERRUPTED_REMOTE_STATE_UNKNOWN"],
        }
    except Exception:
        result = {
            "schema_version": 1,
            "operation": args.operation,
            "status": "failed",
            "reason_codes": ["EXECUTION_FAILED_REMOTE_STATE_UNKNOWN"],
        }
    print(json.dumps(result, ensure_ascii=False))
    return (
        0
        if result["status"]
        in (
            "discovered",
            "inspected",
            "preflight_passed",
            "installed",
            "configured",
            "trust_configured",
            "handoff_prepared",
            "handoff_acknowledged",
            "handoff_cleaned",
            "ready",
        )
        else 3
    )


if __name__ == "__main__":
    raise SystemExit(main())
