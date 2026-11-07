"""Platform entry point: one safe JSON result, never raw Ansible output."""

import argparse
import json

from .config import OPERATIONS, ConfigError, load
from .runner import run


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="OpenBao 탐색·Ubuntu 설치·인증·키 인계"
    )
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args.operation, load(args.config, args.operation))
    except ConfigError as exc:
        result = {
            "schema_version": 1,
            "status": "blocked",
            "reason_codes": ["INVALID_CONFIG"],
            "field_error": str(exc),
        }
    except KeyboardInterrupt:
        result = {
            "schema_version": 1,
            "status": "failed",
            "reason_codes": ["INTERRUPTED"],
        }
    except Exception:  # noqa: BLE001 - boundary must redact provider and credential errors
        result = {
            "schema_version": 1,
            "status": "failed",
            "reason_codes": ["EXECUTION_FAILED"],
        }
    print(json.dumps(result, ensure_ascii=False))
    return (
        0
        if result["status"]
        in (
            "ready",
            "discovered",
            "inspected",
            "preflight_passed",
            "installed",
            "configured",
            "handoff_prepared",
            "handoff_acknowledged",
            "handoff_cleaned",
            "token_issued",
        )
        else 3
    )


if __name__ == "__main__":
    raise SystemExit(main())
