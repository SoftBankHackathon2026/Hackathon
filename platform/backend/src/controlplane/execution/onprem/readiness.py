"""Read-only operator diagnostics; successful API access is not deployment success."""

import argparse
import json
import socket
from pathlib import Path

import psycopg

from controlplane.providers.onprem.connection import connect
from controlplane.providers.onprem.preflight import access_review_body

NAMESPACE = "oneport-apps"
TIMEOUT = (3, 10)


PERMISSION_GROUPS = {
    "application": [
        ("argoproj.io", "rollouts", ("get", "list", "create", "update", "delete")),
        ("argoproj.io", "rollouts/status", ("patch",)),
        ("apps", "replicasets", ("list",)),
        ("", "services", ("get", "create", "delete")),
        ("networking.k8s.io", "ingresses", ("get", "create", "delete")),
        ("", "pods", ("list",)),
        ("", "pods/log", ("get",)),
        ("batch", "jobs", ("get", "create")),
    ],
    "data_services": [
        ("apps", "statefulsets", ("get", "create", "delete")),
        ("apps", "deployments", ("get", "create", "delete")),
        ("", "persistentvolumeclaims", ("get", "create", "delete")),
        ("", "secrets", ("get", "create", "delete")),
    ],
    "complete_deletion": [("batch", "jobs", ("list", "delete"))],
}


def required_permissions():
    return [
        {"scope": scope, "group": group, "resource": resource, "verb": verb}
        for scope, grants in PERMISSION_GROUPS.items()
        for group, resource, verbs in grants
        for verb in verbs
    ]


def permission_report(clients):
    checks = []
    for grant in required_permissions():
        attributes = {k: v for k, v in grant.items() if k != "scope"}
        result = clients.authorization.create_self_subject_access_review(
            body=access_review_body({"namespace": NAMESPACE, **attributes}),
            _request_timeout=TIMEOUT,
        )
        checks.append({**grant, "allowed": result.status.allowed is True})
    return checks


def cluster_dns():
    try:
        socket.getaddrinfo("kubernetes.default.svc.cluster.local", 443)
        return "resolved"
    except OSError:
        return "unresolved"


def database_check(config_file):
    if config_file is None:
        return "not_checked"
    try:
        # Never emit raw config/DSN/driver exceptions: they can contain credentials.
        dsn = json.loads(config_file.read_text())["database_url"]
        with psycopg.connect(dsn, connect_timeout=5, options="-cstatement_timeout=5000") as conn:
            if conn.execute("SELECT 1").fetchone() != (1,):
                return "unreachable_or_invalid_config"
        return "reachable_from_this_process"
    except Exception:  # noqa: BLE001 -- secret-bearing config/driver boundary
        return "unreachable_or_invalid_config"


def inspect(clients, *, database_config=None):
    checks = permission_report(clients)
    counts = {
        "pods": len(clients.core.list_namespaced_pod(NAMESPACE, _request_timeout=TIMEOUT).items),
        "rollouts": len(
            clients.custom.list_namespaced_custom_object(
                "argoproj.io", "v1alpha1", NAMESPACE, "rollouts", _request_timeout=TIMEOUT
            ).get("items", [])
        ),
    }
    clients.core.read_namespaced_service_account(
        "oneport-runtime", NAMESPACE, _request_timeout=TIMEOUT
    )
    dns, db = cluster_dns(), database_check(database_config)
    blockers = []
    if any(not r["allowed"] for r in checks if r["scope"] == "application"):
        blockers.append("application_rbac_missing")
    if dns != "resolved":
        blockers.append("worker_cluster_dns_unresolved")
    if db != "reachable_from_this_process":
        blockers.append("worker_platform_database_not_verified")
    return {
        "namespace": NAMESPACE,
        "api_connected": True,
        "resource_counts": counts,
        "runtime_service_account_present": True,
        "permissions": checks,
        "worker_cluster_dns": dns,
        "platform_database": db,
        "application_execution_blockers": blockers,
        "data_permission_blockers": [
            r for r in checks if r["scope"] != "application" and not r["allowed"]
        ],
        "application_verified": False,
        "unverified": [
            "actual preview Service HTTP and release header",
            "public HTTPS and requested release",
            "node image pull and capacity",
            "PostgreSQL/Redis authenticated application requests",
        ],
        "note": "Run inside the actual worker environment; local DB/DNS results do not prove cluster worker access.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", default="oneport-onprem")
    parser.add_argument("--server", default="https://kube-api.yubin.website")
    parser.add_argument("--database-config", type=Path)
    args = parser.parse_args()
    clients = None
    try:
        clients = connect(
            kubeconfig=args.kubeconfig, context=args.context, expected_server=args.server
        )
        result = inspect(clients, database_config=args.database_config)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1 if result["application_execution_blockers"] else 0
    except Exception as exc:  # noqa: BLE001 -- never expose SDK response bodies or secrets
        print(
            json.dumps(
                {
                    "error": type(exc).__name__,
                    "message": "Check operator configuration and cluster access",
                }
            )
        )
        return 1
    finally:
        if clients:
            clients.close()


if __name__ == "__main__":
    raise SystemExit(main())
