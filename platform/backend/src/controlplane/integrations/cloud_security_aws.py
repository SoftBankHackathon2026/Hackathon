"""AWS 보안 실측. 읽기 전용 API 만 부른다(필요 권한: platform/docs/SECURITY-CLOUD-PERMISSIONS.md).

boto3 는 aws 추가 의존성이다. 이 모듈을 import 할 때 boto3 가 없으면 ImportError 가 난다.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.config import Config

from controlplane.domain.cloud_security import CloudSecurityFacts, Observation
from controlplane.domain.fleet import EnvironmentConfig
from controlplane.integrations.cloud_security import attempt, guarded

# 한 호출이 수집 주기를 잡아먹지 않게 짧게 끊고, 재시도는 한 번만 더 한다.
CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2})

# 엔진별 "TLS 연결만 허용" 파라미터. Oracle 은 파라미터가 아니라 옵션 그룹이라 다루지 않는다.
TLS_PARAMETERS = {
    "postgres": "rds.force_ssl",
    "aurora-postgresql": "rds.force_ssl",
    "mysql": "require_secure_transport",
    "mariadb": "require_secure_transport",
    "aurora-mysql": "require_secure_transport",
}
ON_VALUES = {"1", "on", "true"}

Session = Any


def tls_parameter(engine: str) -> str | None:
    if engine.startswith("sqlserver"):
        return "rds.force_ssl"
    return TLS_PARAMETERS.get(engine)


def _summary(items: list[str]) -> str:
    return ", ".join(items)


class AwsSecurityReader:
    def __init__(self, session: Callable[[str], Session] | None = None) -> None:
        self._session = session or (lambda region: boto3.session.Session(region_name=region))

    def read(self, env: EnvironmentConfig) -> CloudSecurityFacts:
        scope = env.cloud_scope
        assert scope is not None
        session = self._session(env.region)

        def client(name: str) -> Any:
            return session.client(name, region_name=env.region, config=CLIENT_CONFIG)

        facts = self._databases(client, scope.databases)
        facts["threat_detection"] = guarded(
            "guardduty:ListDetectors, guardduty:GetDetector",
            lambda: self._guardduty(client("guardduty"), env.region),
        )
        facts["flow_logs"] = guarded(
            "eks:DescribeCluster, ec2:DescribeFlowLogs",
            lambda: self._flow_logs(client("eks"), client("ec2"), scope.cluster),
        )
        return CloudSecurityFacts(observed_at=datetime.now(UTC), provider="aws", facts=facts)

    # DB: NET-02, DATA-01~03

    def _databases(self, client: Callable[[str], Any], names: list[str]) -> dict:
        db_facts = ("db_public", "db_tls_required", "db_encrypted_at_rest", "db_backups")
        if not names:
            missing = Observation.failed("unconfigured", "cloud_scope.databases 가 비어 있음")
            return {fact: missing for fact in db_facts}
        rds = client("rds")
        instances, failed = attempt(
            "rds:DescribeDBInstances",
            lambda: [
                rds.describe_db_instances(DBInstanceIdentifier=name)["DBInstances"][0]
                for name in names
            ],
        )
        if instances is None:
            return {fact: failed for fact in db_facts}
        return {
            "db_public": Observation.seen(
                any(i.get("PubliclyAccessible") for i in instances),
                _summary(
                    [
                        f"{i['DBInstanceIdentifier']} PubliclyAccessible={i.get('PubliclyAccessible')}"
                        for i in instances
                    ]
                ),
            ),
            "db_encrypted_at_rest": Observation.seen(
                all(i.get("StorageEncrypted") for i in instances),
                _summary(
                    [
                        f"{i['DBInstanceIdentifier']} StorageEncrypted={i.get('StorageEncrypted')}"
                        for i in instances
                    ]
                ),
            ),
            "db_backups": Observation.seen(
                all((i.get("BackupRetentionPeriod") or 0) > 0 for i in instances),
                _summary(
                    [
                        f"{i['DBInstanceIdentifier']} 백업 보존 {i.get('BackupRetentionPeriod') or 0}일"
                        for i in instances
                    ]
                ),
            ),
            "db_tls_required": guarded(
                "rds:DescribeDBParameters, rds:DescribeDBClusters, rds:DescribeDBClusterParameters",
                lambda: self._tls(rds, instances),
            ),
        }

    def _tls(self, rds: Any, instances: list[dict]) -> Observation:
        verdicts, evidence = [], []
        for instance in instances:
            name = instance["DBInstanceIdentifier"]
            engine = instance.get("Engine") or ""
            parameter = tls_parameter(engine)
            if parameter is None:
                return Observation.failed(
                    "not_found", f"{name}: {engine} 엔진의 TLS 파라미터를 모름"
                )
            value, group = self._parameter(rds, instance, parameter)
            if value is None:
                return Observation.failed(
                    "not_found", f"{name}: {group} 에서 {parameter} 값을 읽지 못함"
                )
            verdicts.append(value.lower() in ON_VALUES)
            evidence.append(f"{name} {group} {parameter}={value}")
        return Observation.seen(all(verdicts), _summary(evidence))

    def _parameter(self, rds: Any, instance: dict, name: str) -> tuple[str | None, str]:
        cluster = instance.get("DBClusterIdentifier")
        if cluster and (instance.get("Engine") or "").startswith("aurora"):
            found = rds.describe_db_clusters(DBClusterIdentifier=cluster)["DBClusters"][0]
            group = found["DBClusterParameterGroup"]
            pages = rds.get_paginator("describe_db_cluster_parameters").paginate(
                DBClusterParameterGroupName=group
            )
        else:
            group = instance["DBParameterGroups"][0]["DBParameterGroupName"]
            pages = rds.get_paginator("describe_db_parameters").paginate(DBParameterGroupName=group)
        for page in pages:
            for parameter in page.get("Parameters", []):
                if parameter.get("ParameterName") == name:
                    return parameter.get("ParameterValue"), group
        return None, group

    # DET-01

    def _guardduty(self, guardduty: Any, region: str) -> Observation:
        detectors = guardduty.list_detectors().get("DetectorIds") or []
        if not detectors:
            return Observation.seen(False, f"{region} 에 GuardDuty 디텍터 없음")
        statuses = {d: guardduty.get_detector(DetectorId=d).get("Status") for d in detectors}
        enabled = [d for d, status in statuses.items() if status == "ENABLED"]
        evidence = _summary([f"디텍터 {d} {status}" for d, status in statuses.items()])
        return Observation.seen(bool(enabled), evidence)

    # DET-04

    def _flow_logs(self, eks: Any, ec2: Any, cluster: str) -> Observation:
        config = eks.describe_cluster(name=cluster)["cluster"]["resourcesVpcConfig"]
        vpc, subnets = config["vpcId"], list(config.get("subnetIds") or [])
        logs = ec2.describe_flow_logs(
            Filters=[{"Name": "resource-id", "Values": [vpc, *subnets]}]
        ).get("FlowLogs", [])
        active = {log["ResourceId"] for log in logs if log.get("FlowLogStatus") == "ACTIVE"}
        if vpc in active:
            return Observation.seen(True, f"{cluster} VPC {vpc} 흐름 로그 ACTIVE")
        if subnets and set(subnets) <= active:
            return Observation.seen(
                True, f"{cluster} 서브넷 {len(subnets)}개 모두 흐름 로그 ACTIVE"
            )
        uncovered = [s for s in subnets if s not in active]
        return Observation.seen(
            False, f"{cluster} VPC {vpc} 흐름 로그 없음(미적용 서브넷 {len(uncovered)}개)"
        )
