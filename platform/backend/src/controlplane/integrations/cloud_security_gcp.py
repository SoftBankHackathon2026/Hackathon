"""GCP 보안 실측. REST 읽기(GET)만 부른다.

필요 권한: cloudsql.instances.get, container.clusters.get, compute.subnetworks.get,
securitycentermanagement.securityCenterServices.get. 해당 API 가 꺼져 있으면 403 SERVICE_DISABLED.

google-auth 와 httpx 는 기본 의존성이라 GCP 전용 클라이언트 라이브러리를 더하지 않는다.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from controlplane.domain.cloud_security import CloudSecurityFacts, Observation
from controlplane.domain.fleet import EnvironmentConfig
from controlplane.integrations.cloud_security import attempt, guarded

GCP_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
SQLADMIN = "https://sqladmin.googleapis.com/v1"
CONTAINER = "https://container.googleapis.com/v1"
COMPUTE = "https://compute.googleapis.com/compute/v1"
SCC_MANAGEMENT = "https://securitycentermanagement.googleapis.com/v1"
TLS_ONLY = {"ENCRYPTED_ONLY", "TRUSTED_CLIENT_CERTIFICATE_REQUIRED"}
TIMEOUT_SECONDS = 10

Http = Any


def default_http() -> Http:
    import google.auth
    import google.auth.transport.requests
    import httpx

    credentials, _ = google.auth.default(scopes=[GCP_SCOPE])
    credentials.refresh(google.auth.transport.requests.Request())
    return httpx.Client(
        timeout=TIMEOUT_SECONDS, headers={"Authorization": f"Bearer {credentials.token}"}
    )


class GcpSecurityReader:
    def __init__(self, http: Callable[[], Http] = default_http) -> None:
        self._http = http

    def read(self, env: EnvironmentConfig) -> CloudSecurityFacts:
        scope = env.cloud_scope
        assert scope is not None
        if not scope.project:
            missing = Observation.failed("unconfigured", "cloud_scope.project 가 없음")
            facts = {
                fact: missing
                for fact in (
                    "db_public",
                    "db_tls_required",
                    "db_encrypted_at_rest",
                    "db_backups",
                    "threat_detection",
                    "flow_logs",
                )
            }
            return CloudSecurityFacts(observed_at=datetime.now(UTC), provider="gcp", facts=facts)
        http = self._http()

        def get(url: str) -> dict[str, Any]:
            return http.get(url).raise_for_status().json()

        project = scope.project
        facts = self._databases(get, project, scope.databases)
        facts["threat_detection"] = guarded(
            "securitycentermanagement.securityCenterServices.get",
            lambda: self._threat_detection(get, project),
        )
        location = scope.cluster_location or env.region
        facts["flow_logs"] = guarded(
            "container.clusters.get, compute.subnetworks.get",
            lambda: self._flow_logs(get, project, location, scope.cluster),
        )
        return CloudSecurityFacts(observed_at=datetime.now(UTC), provider="gcp", facts=facts)

    # DB: NET-02, DATA-01~03

    def _databases(self, get: Callable[[str], dict], project: str, names: list[str]) -> dict:
        db_facts = ("db_public", "db_tls_required", "db_encrypted_at_rest", "db_backups")
        if not names:
            missing = Observation.failed("unconfigured", "cloud_scope.databases 가 비어 있음")
            return {fact: missing for fact in db_facts}
        instances, failed = attempt(
            "cloudsql.instances.get",
            lambda: [get(f"{SQLADMIN}/projects/{project}/instances/{name}") for name in names],
        )
        if instances is None:
            return {fact: failed for fact in db_facts}
        return {
            "db_public": _public(instances),
            "db_tls_required": _tls(instances),
            "db_encrypted_at_rest": _encryption(instances),
            "db_backups": _backups(instances),
        }

    # DET-01

    def _threat_detection(self, get: Callable[[str], dict], project: str) -> Observation:
        name = (
            f"{SCC_MANAGEMENT}/projects/{project}/locations/global/"
            "securityCenterServices/event-threat-detection"
        )
        state = get(name).get("effectiveEnablementState")
        if state == "ENABLED":
            return Observation.seen(True, "SCC Event Threat Detection ENABLED")
        if state == "DISABLED":
            return Observation.seen(False, "SCC Event Threat Detection DISABLED")
        return Observation.failed("error", f"SCC Event Threat Detection 상태 {state!r}")

    # DET-04

    def _flow_logs(
        self, get: Callable[[str], dict], project: str, location: str, cluster: str
    ) -> Observation:
        found = get(f"{CONTAINER}/projects/{project}/locations/{location}/clusters/{cluster}")
        subnetwork = (found.get("networkConfig") or {}).get("subnetwork")
        if not subnetwork:
            return Observation.failed("not_found", f"{cluster} 의 서브넷을 찾지 못함")
        subnet = get(f"{COMPUTE}/{subnetwork}")
        enabled = bool(
            (subnet.get("logConfig") or {}).get("enable") or subnet.get("enableFlowLogs")
        )
        short = subnetwork.rsplit("/", 1)[-1]
        return Observation.seen(
            enabled, f"{cluster} 서브넷 {short} 흐름 로그 {'켜짐' if enabled else '꺼짐'}"
        )


def _ip(instance: dict) -> dict:
    return (instance.get("settings") or {}).get("ipConfiguration") or {}


def _public(instances: list[dict]) -> Observation:
    evidence = []
    for instance in instances:
        ip = _ip(instance)
        networks = [n.get("value") for n in ip.get("authorizedNetworks") or []]
        text = f"{instance['name']} 공인 IP {'켜짐' if ip.get('ipv4Enabled') else '꺼짐'}"
        if ip.get("ipv4Enabled"):
            open_world = "0.0.0.0/0" in networks
            text += f", 허용 네트워크 {len(networks)}개" + (
                " (0.0.0.0/0 포함)" if open_world else ""
            )
        evidence.append(text)
    public = any(_ip(i).get("ipv4Enabled") for i in instances)
    return Observation.seen(public, ", ".join(evidence))


def _tls(instances: list[dict]) -> Observation:
    verdicts, evidence = [], []
    for instance in instances:
        ip = _ip(instance)
        mode = ip.get("sslMode")
        if mode:
            verdicts.append(mode in TLS_ONLY)
            evidence.append(f"{instance['name']} sslMode={mode}")
        else:
            verdicts.append(bool(ip.get("requireSsl")))
            evidence.append(f"{instance['name']} requireSsl={bool(ip.get('requireSsl'))}")
    return Observation.seen(all(verdicts), ", ".join(evidence))


def _encryption(instances: list[dict]) -> Observation:
    """Cloud SQL 은 저장 데이터를 항상 암호화한다. CMEK 여부는 근거에만 남긴다."""
    evidence = []
    for instance in instances:
        key = (instance.get("diskEncryptionConfiguration") or {}).get("kmsKeyName")
        evidence.append(f"{instance['name']} " + (f"CMEK {key}" if key else "Google 관리 키"))
    return Observation.seen(True, "기본 암호화: " + ", ".join(evidence))


def _backups(instances: list[dict]) -> Observation:
    def enabled(instance: dict) -> bool:
        backup = (instance.get("settings") or {}).get("backupConfiguration") or {}
        return bool(backup.get("enabled"))

    evidence = [f"{i['name']} 자동 백업 {'켜짐' if enabled(i) else '꺼짐'}" for i in instances]
    return Observation.seen(all(enabled(i) for i in instances), ", ".join(evidence))
