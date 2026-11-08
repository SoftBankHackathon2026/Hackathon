import argparse
import json

import yaml

from ..wazuh_infra.config import ConfigError
from .config import OPERATIONS, load
from .runner import run

SUCCESS = ("planned", "ready")


def main(argv=None, session=None):
    parser = argparse.ArgumentParser(
        description="AWS 이전 대상 계정·리전 읽기 전용 점검"
    )
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args.operation, load(args.config), session)
    except ConfigError as error:
        result = {
            "status": "blocked",
            "reason_codes": ["INVALID_CONFIG"],
            "field_error": str(error),
        }
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        result = {"status": "blocked", "reason_codes": ["INPUT_INVALID_OR_UNREADABLE"]}
    except KeyboardInterrupt:
        result = {"status": "failed", "reason_codes": ["INTERRUPTED"]}
    except Exception:
        result = {"status": "failed", "reason_codes": ["EXECUTION_FAILED"]}
    print(
        json.dumps(
            {"schema_version": 1, "operation": args.operation, **result},
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0 if result["status"] in SUCCESS else 3


if __name__ == "__main__":
    raise SystemExit(main())
