"""Machine-readable local entry point, called only by the authorized agent."""

from __future__ import annotations

import argparse
import json
from functools import partial

from .cloud import collect_cloud, discover_cloud
from .collector import InventoryError, Kubectl, collect, discover, run_bounded
from .observation import MAX_NAMESPACES, observe
from .schema import inventory_digest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="선택한 Kubernetes 환경의 읽기 전용 정보 수집"
    )
    parser.add_argument("operation", choices=("discover", "collect", "observe"))
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--epoch", required=True, type=int)
    parser.add_argument("--target-id")
    parser.add_argument(
        "--provider", choices=("kubernetes", "aws", "gcp"), default="kubernetes"
    )
    parser.add_argument("--cloud-context")
    parser.add_argument("--namespace-uid")
    parser.add_argument("--namespaces", default="")
    args = parser.parse_args(argv)
    try:
        binding = {
            "request_id": args.request_id,
            "device_id": args.device_id,
            "epoch": args.epoch,
        }
        if args.operation == "discover":
            result = (
                discover(Kubectl(), **binding)
                if args.provider == "kubernetes"
                else discover_cloud(args.provider)
            )
        elif args.operation == "observe":
            namespaces = args.namespaces.split(",") if args.namespaces else []
            if (
                args.provider != "kubernetes"
                or not args.target_id
                or not args.namespace_uid
                or len(namespaces) > MAX_NAMESPACES
            ):
                raise InventoryError("INVALID_OBSERVATION_SCOPE")
            result = {
                "report": observe(
                    Kubectl(runner=partial(run_bounded, timeout=8)),
                    target_id=args.target_id,
                    namespace_uid=args.namespace_uid,
                    namespaces=namespaces,
                )
            }
        else:
            if not args.target_id:
                raise InventoryError("TARGET_SELECTION_REQUIRED")
            report = (
                collect(Kubectl(), selected_target_id=args.target_id, **binding)
                if args.provider == "kubernetes"
                else collect_cloud(
                    args.provider, args.cloud_context or "", args.target_id, **binding
                )
            )
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
