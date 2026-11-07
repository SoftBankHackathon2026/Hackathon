"""Machine-readable local entry point, called only by the authorized agent."""

from __future__ import annotations

import argparse
import json

from .collector import InventoryError, Kubectl, collect, discover
from .schema import inventory_digest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="선택한 Kubernetes 환경의 읽기 전용 정보 수집"
    )
    parser.add_argument("operation", choices=("discover", "collect"))
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--epoch", required=True, type=int)
    parser.add_argument("--target-id")
    args = parser.parse_args(argv)
    try:
        client = Kubectl()
        binding = {
            "request_id": args.request_id,
            "device_id": args.device_id,
            "epoch": args.epoch,
        }
        if args.operation == "discover":
            result = discover(client, **binding)
        else:
            if not args.target_id:
                raise InventoryError("TARGET_SELECTION_REQUIRED")
            report = collect(client, selected_target_id=args.target_id, **binding)
            result = {"report": report, "inventory_digest": inventory_digest(report)}
    except InventoryError as error:
        print(
            json.dumps(
                {"schema_version": 1, "status": "blocked", "reason_code": error.code}
            )
        )
        return 2
    except Exception:  # noqa: BLE001 - Process boundary must redact credential-plugin exceptions.
        # Credential plugins and kubectl may contain secrets in exceptions;
        # the process boundary deliberately exposes no exception text.
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "failed",
                    "reason_code": "INVENTORY_EXECUTION_FAILED",
                }
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
