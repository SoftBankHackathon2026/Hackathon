"""Read-only prerequisites from the Worker host; never enqueue, migrate or deploy."""

import argparse
import json
import socket
from pathlib import Path

from controlplane.execution.onprem.settings import ArgoSettings
from controlplane.providers.onprem import ArgoProvider
from controlplane.providers.onprem.connection import connect
from controlplane.providers.onprem.models import ArgoDeploymentRequest
from controlplane.providers.onprem.preflight import PreflightReport, preflight
from controlplane.repositories.releases import ReleaseStore


def check_database(settings):
    # Use the exact Worker DSN. No schema creation or writes, including on a fresh DB.
    with ReleaseStore(settings.database_url.get_secret_value()).connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '3s'")
        conn.execute("SELECT id, runtime, next_attempt_at FROM releases LIMIT 0")
        conn.execute("SELECT release_id, state FROM release_events LIMIT 0")


def check_access(settings, request) -> PreflightReport:
    clients = connect(
        kubeconfig=settings.kubeconfig,
        context=settings.context,
        expected_server=settings.server,
    )
    try:
        return preflight(
            ArgoProvider(clients, settings.environment),
            request,
            preview_job=settings.preview_probe_mode == "job",
        )
    finally:
        clients.close()


def check_endpoint(host):
    # Checks name resolution and one ClusterIP/public TCP path, not application health.
    with socket.create_connection((host, 443), timeout=3):
        pass


UNVERIFIED = [
    "Worker daemon running and consuming this queue",
    "Node image pull and available capacity",
    "Argo controller reconciliation and preview application HTTP",
    "Public TLS and application release response",
    "Application DB/Redis connectivity (if requested)",
]
PERMISSIONS_CHECK = "kubernetes_permissions_and_bindings"


class Checks:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def run(self, name: str, action, hint: str) -> bool:
        try:
            action()
        except Exception as exc:
            self.results.append(
                {"name": name, "passed": False, "error_type": type(exc).__name__, "hint": hint}
            )
            return False
        self.results.append({"name": name, "passed": True})
        return True

    def record_permissions(self, report: PreflightReport) -> None:
        if report.ready_for_sdk:
            self.results.append({"name": PERMISSIONS_CHECK, "passed": True})
            return
        denied = [p.model_dump(exclude_none=True) for p in report.permissions if not p.allowed]
        self.results.append(
            {"name": PERMISSIONS_CHECK, "passed": False, "code": report.code, "denied": denied}
        )

    def record_note(self, name: str, note: str) -> None:
        self.results.append({"name": name, "passed": True, "note": note})


def _check_cluster_reachability(checks: Checks, settings, endpoint_check) -> None:
    if settings.preview_probe_mode == "http":
        checks.run(
            "cluster_service_dns_and_tcp",
            lambda: endpoint_check(f"kubernetes.default.svc.{settings.environment.cluster_domain}"),
            "현재 Worker 위치에서 클러스터 DNS/Service에 닿지 않습니다. API IP 접속만으로는 부족합니다.",
        )
    else:
        checks.record_note(
            "preview_job_configured", "Job 내부 DNS/HTTP 실행은 실제 배포에서 검증합니다."
        )


def diagnose(settings, request, *, database_check=None, access_check=None, endpoint_check=None):
    database_check = database_check or check_database
    access_check = access_check or check_access
    endpoint_check = endpoint_check or check_endpoint
    checks = Checks()
    valid = checks.run(
        "request_policy",
        lambda: settings.validate_request(request),
        "등록 앱·환경·digest allowlist·자원 제한·Secret UID를 확인하세요.",
    )
    if valid:
        checks.run(
            "platform_database",
            lambda: database_check(settings),
            "Worker에서 플랫폼 DB에 접근해야 합니다. API를 시작해 이력 스키마를 준비하세요.",
        )
        checks.run(
            "kubernetes_preflight_request",
            lambda: checks.record_permissions(access_check(settings, request)),
            "kubeconfig·CA·토큰·API 경로를 확인하세요.",
        )
        _check_cluster_reachability(checks, settings, endpoint_check)
        checks.run(
            "public_hostname_dns_and_tcp",
            lambda: endpoint_check(settings.environment.hostname),
            "할당한 앱 hostname의 DNS와 공개 443 경로를 확인하세요.",
        )
    return {
        "checks_passed": all(c["passed"] for c in checks.results),
        "application_verified": False,
        "checks": checks.results,
        "unverified": UNVERIFIED,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        settings = ArgoSettings.model_validate_json(args.config.read_text())
        request = ArgoDeploymentRequest.model_validate_json(args.request.read_text())
        result = diagnose(settings, request)
    except Exception as exc:
        result = {
            "checks_passed": False,
            "application_verified": False,
            "error_type": type(exc).__name__,
            "message": "설정·요청 파일을 확인하세요. 비밀값은 출력하지 않습니다.",
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
