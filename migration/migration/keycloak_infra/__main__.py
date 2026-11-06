"""One JSON result on stdout, with stable process exit semantics."""

from __future__ import annotations

import argparse
import json

from .config import OPERATIONS, ConfigError, load_config
from .runner import private_json, run


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Keycloak 환경 탐색·점검·호스트 설치 (계정·정책 이관 제외)"
    )
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("--config", required=True)
    parser.add_argument("--result", help="비밀 원문 없는 JSON 결과 파일")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config, args.operation)
        result = run(args.operation, config)
    except ConfigError as error:
        result = {
            "schema_version": 1,
            "operation": args.operation,
            "status": "blocked",
            "reason_codes": ["INVALID_CONFIG"],
            "message": str(error),
        }
    except (OSError, ValueError, RuntimeError):
        result = {
            "schema_version": 1,
            "operation": args.operation,
            "status": "failed",
            "reason_codes": ["CONTROLLER_EXECUTION_ERROR"],
        }
    if args.result:
        try:
            private_json(args.result, result)
        except OSError:
            result = {
                "schema_version": 1,
                "operation": args.operation,
                "status": "failed",
                "reason_codes": ["RESULT_WRITE_FAILED"],
            }
    print(json.dumps(result, ensure_ascii=False))
    return {"complete": 0, "failed": 1, "blocked": 2, "partial": 3}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
