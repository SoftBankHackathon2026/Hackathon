"""Render or read-only preflight; never deploy through this tool."""

import argparse
import json
from pathlib import Path

from controlplane.providers.contracts import ReleaseKey
from controlplane.providers.onprem import ArgoEnvironment, ArgoProvider
from controlplane.providers.onprem.connection import connect
from controlplane.providers.onprem.manifests import bundle
from controlplane.providers.onprem.models import ArgoDeploymentRequest
from controlplane.providers.onprem.preflight import preflight

REVIEW_RELEASE = ReleaseKey(value="a" * 24)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["render", "preflight"])
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--context")
    parser.add_argument("--server")
    return parser


def run_preflight(args, env: ArgoEnvironment, request: ArgoDeploymentRequest) -> int:
    clients = connect(kubeconfig=args.kubeconfig, context=args.context, expected_server=args.server)
    try:
        report = preflight(ArgoProvider(clients, env), request)
    finally:
        clients.close()
    print(json.dumps(report.to_json(), indent=2))
    return 0 if report.ready_for_sdk else 1


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        env = ArgoEnvironment.model_validate_json(args.environment.read_text())
        request = ArgoDeploymentRequest.model_validate_json(args.request.read_text())
        if args.command == "render":
            print(json.dumps(bundle(request, env, REVIEW_RELEASE), indent=2))
            return 0
        if not all((args.kubeconfig, args.context, args.server)):
            parser.error("preflight requires explicit --kubeconfig --context --server")
        return run_preflight(args, env, request)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary: don't disclose credentials
        print(
            json.dumps(
                {"error": type(exc).__name__, "message": "Check operator inputs and cluster access"}
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
