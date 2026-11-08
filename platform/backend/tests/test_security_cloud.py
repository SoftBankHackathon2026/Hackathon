from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError

from controlplane.domain.cloud_security import (
    OBSERVED_FACTS,
    CloudSecurityFacts,
    Observation,
)
from controlplane.domain.fleet import ClusterFacts, EnvironmentSnapshot, FleetConfig
from controlplane.domain.security_controls import SecurityPolicy, evaluate
from controlplane.execution.fleet_collector import FleetCollector
from controlplane.integrations.cloud_security import (
    StaticCloudSecurityReader,
    classify,
    collect_cloud_security,
)
from controlplane.integrations.cloud_security_aws import AwsSecurityReader
from controlplane.integrations.cloud_security_gcp import GcpSecurityReader
from controlplane.integrations.defaults import AWS, DEFAULT_FLEET, GCP, ONPREM
from controlplane.integrations.simulator import SimulatedFleet
from controlplane.services.security import SecurityService

NOW = datetime(2026, 11, 8, tzinfo=UTC)
POLICY = SecurityPolicy(
    japan_regions=frozenset({"ap-northeast-1"}), k8s_min_minor="1.35", k8s_max_node_skew=1
)
DISCONNECTED = ClusterFacts(observed_at=None, connected=False)

# (control, fact, 좋은 값). db_public 은 True 가 나쁜 값이다.
CASES = [
    ("NET-02", "db_public", False),
    ("DATA-01", "db_tls_required", True),
    ("DATA-02", "db_encrypted_at_rest", True),
    ("DATA-03", "db_backups", True),
    ("DET-01", "threat_detection", True),
    ("DET-04", "flow_logs", True),
]


def aws_error(code: str, op: str = "Call") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "fake"}}, op)


def with_declared(env, **values):
    return env.model_copy(update={"declared": env.declared.model_copy(update=values)})


def result(env, control_id, cloud):
    return evaluate(EnvironmentSnapshot(env, DISCONNECTED, cloud), POLICY)[control_id]


def cloud(provider="aws", **facts):
    return CloudSecurityFacts(observed_at=NOW, provider=provider, facts=facts)


# 컨트롤별 병합: pass / fail / 권한 부족 unknown / 신고값 폴백


@pytest.mark.parametrize(("control_id", "fact", "good"), CASES)
def test_observed_good_value_passes_as_observed(control_id, fact, good):
    got = result(AWS, control_id, cloud(**{fact: Observation.seen(good, "근거")}))
    assert (got.status, got.source, got.fallback_reason) == ("pass", "observed", None)
    assert "근거" in got.detail


@pytest.mark.parametrize(("control_id", "fact", "good"), CASES)
def test_observed_bad_value_fails_as_observed(control_id, fact, good):
    got = result(AWS, control_id, cloud(**{fact: Observation.seen(not good, "근거")}))
    assert (got.status, got.source) == ("fail", "observed")


@pytest.mark.parametrize(("control_id", "fact", "good"), CASES)
def test_access_denied_without_declared_value_is_unknown_with_reason(control_id, fact, good):
    env = with_declared(AWS, **{fact: None})
    denied = cloud(**{fact: Observation.failed("denied", "rds:DescribeDBInstances")})
    got = result(env, control_id, denied)
    assert got.status == "unknown"
    assert got.source is None
    assert got.fallback_reason == "권한 없음: rds:DescribeDBInstances"
    assert "권한 없음" in got.detail


@pytest.mark.parametrize(("control_id", "fact", "good"), CASES)
def test_failed_observation_falls_back_to_declared_with_reason(control_id, fact, good):
    env = with_declared(AWS, **{fact: good})
    timed_out = cloud(**{fact: Observation.failed("timeout", "guardduty:ListDetectors")})
    got = result(env, control_id, timed_out)
    assert (got.status, got.source) == ("pass", "declared")
    assert got.fallback_reason == "응답 시간 초과: guardduty:ListDetectors"


@pytest.mark.parametrize(("control_id", "fact", "good"), CASES)
def test_observed_value_wins_over_conflicting_declared_value(control_id, fact, good):
    env = with_declared(AWS, **{fact: good})
    got = result(env, control_id, cloud(**{fact: Observation.seen(not good, "실제 설정")}))
    assert (got.status, got.source) == ("fail", "observed")


def test_missing_collection_falls_back_with_reason_but_onprem_does_not():
    aws = result(AWS, "DET-04", None)
    assert (aws.source, aws.fallback_reason) == (
        "declared",
        "실측 결과 없음(수집기가 아직 읽지 않았어요)",
    )
    onprem = result(ONPREM, "DATA-01", None)
    assert (onprem.status, onprem.source, onprem.fallback_reason) == ("pass", "declared", None)


def test_other_controls_keep_their_source():
    results = evaluate(EnvironmentSnapshot(AWS, DISCONNECTED, None), POLICY)
    assert results["NET-03"].source == "declared"  # 신고값만 쓰는 통제
    assert results["JP-01"].source == "declared"  # fleet.json 리전
    assert results["NET-01"].source is None  # 클러스터 미연결 unknown


# 수집 단위: 오류 격리


def test_reader_exception_marks_every_fact_failed_and_falls_back():
    class Broken:
        def read(self, env):
            raise aws_error("AccessDeniedException")

    facts = collect_cloud_security(AWS, {"aws": Broken()}, now=lambda: NOW)
    assert set(facts.facts) == set(OBSERVED_FACTS)
    assert all(o.failure == "denied" for o in facts.facts.values())
    assert result(AWS, "DATA-02", facts).source == "declared"


def test_onprem_is_not_collected_and_missing_scope_is_reported():
    assert collect_cloud_security(ONPREM, {}) is None
    unscoped = AWS.model_copy(update={"cloud_scope": None})
    facts = collect_cloud_security(unscoped, {"aws": StaticCloudSecurityReader({})})
    assert facts.facts["db_public"].failure == "unconfigured"


def test_reader_omitting_a_fact_is_filled_as_error():
    reader = StaticCloudSecurityReader({"db_public": Observation.seen(False, "")})
    facts = collect_cloud_security(AWS, {"aws": reader})
    assert facts.facts["db_public"].value is False
    assert facts.facts["flow_logs"].failure == "error"


def test_collector_keeps_collecting_when_one_provider_reader_raises():
    stored = {}

    class Journal:
        def put_cloud_security(self, env_id, at, facts):
            stored[env_id] = facts

    class Broken:
        def read(self, env):
            raise RuntimeError("boom")

    gcp = StaticCloudSecurityReader({"flow_logs": Observation.seen(True, "켜짐")}, "gcp")
    collector = FleetCollector(
        FleetConfig(mode="live", environments=[ONPREM, AWS, GCP], migrations=[]),
        journal=Journal(),
        clock=lambda: 1000.0,
        cloud_readers={"aws": Broken(), "gcp": gcp},
    )
    collector.collect_environment = lambda env: None
    collector.tick()
    assert set(stored) == {"aws", "gcp"}
    assert stored["aws"]["facts"]["db_public"]["failure"] == "error"
    assert stored["gcp"]["facts"]["flow_logs"]["value"] is True


def test_collector_follows_cloud_interval():
    clock = SimpleNamespace(now=1000.0)
    runs = []
    collector = FleetCollector(
        FleetConfig(mode="live", environments=[AWS], migrations=[]),
        journal=None,
        clock=lambda: clock.now,
        cloud_readers={},
        cloud_seconds=300,
    )
    collector.collect_environment = lambda env: None
    collector.collect_cloud_security = lambda env: runs.append(clock.now)
    for _ in range(4):
        collector.tick()
        clock.now += 150
    assert runs == [1000.0, 1300.0]


@pytest.mark.parametrize(
    ("exc", "failure"),
    [
        (aws_error("AccessDenied"), "denied"),
        (aws_error("UnauthorizedOperation"), "denied"),
        (aws_error("ExpiredToken"), "no_credentials"),
        (aws_error("DBInstanceNotFound"), "not_found"),
        (aws_error("Throttling"), "error"),
        (ConnectTimeoutError(endpoint_url="https://rds"), "timeout"),
        (httpx.ReadTimeout("slow"), "timeout"),
    ],
)
def test_classify_errors(exc, failure):
    assert classify(exc, "action")[0] == failure


# AWS 리더 (가짜 boto3 세션)


class FakeAws:
    def __init__(
        self,
        *,
        db=None,
        force_ssl="1",
        detectors=("d1",),
        status="ENABLED",
        flow_logs=(("vpc-1", "ACTIVE"),),
        fail=None,
    ):
        self.db = db or {
            "DBInstanceIdentifier": "oneaction-review-db",
            "Engine": "postgres",
            "PubliclyAccessible": False,
            "StorageEncrypted": True,
            "BackupRetentionPeriod": 7,
            "DBParameterGroups": [{"DBParameterGroupName": "oneaction-postgres16"}],
        }
        self.force_ssl = force_ssl
        self.detectors = list(detectors)
        self.status = status
        self.flow_logs = flow_logs
        self.fail = fail or {}

    def client(self, name, **_):
        return FakeClient(self, name)


class FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_):
        return iter(self._pages)


class FakeClient:
    def __init__(self, aws, name):
        self.aws, self.name = aws, name

    def _check(self, op):
        if op in self.aws.fail:
            raise self.aws.fail[op]

    def describe_db_instances(self, DBInstanceIdentifier):
        self._check("describe_db_instances")
        return {"DBInstances": [self.aws.db]}

    def get_paginator(self, op):
        self._check(op)
        value = self.aws.force_ssl
        params = [{"ParameterName": "rds.force_ssl", "ParameterValue": value}] if value else []
        return FakePaginator([{"Parameters": [{"ParameterName": "other"}]}, {"Parameters": params}])

    def list_detectors(self):
        self._check("list_detectors")
        return {"DetectorIds": self.aws.detectors}

    def get_detector(self, DetectorId):
        return {"Status": self.aws.status}

    def describe_cluster(self, name):
        self._check("describe_cluster")
        return {"cluster": {"resourcesVpcConfig": {"vpcId": "vpc-1", "subnetIds": ["s-1", "s-2"]}}}

    def describe_flow_logs(self, Filters):
        self._check("describe_flow_logs")
        return {"FlowLogs": [{"ResourceId": r, "FlowLogStatus": s} for r, s in self.aws.flow_logs]}


def read_aws(fake):
    return AwsSecurityReader(session=lambda region: fake).read(AWS).facts


def test_aws_reader_maps_healthy_account():
    facts = read_aws(FakeAws())
    assert {k: v.value for k, v in facts.items()} == {
        "db_public": False,
        "db_tls_required": True,
        "db_encrypted_at_rest": True,
        "db_backups": True,
        "threat_detection": True,
        "flow_logs": True,
    }
    assert facts["db_tls_required"].evidence == (
        "oneaction-review-db oneaction-postgres16 rds.force_ssl=1"
    )


def test_aws_reader_maps_weak_account():
    weak_db = {
        "DBInstanceIdentifier": "db",
        "Engine": "mysql",
        "PubliclyAccessible": True,
        "StorageEncrypted": False,
        "BackupRetentionPeriod": 0,
        "DBParameterGroups": [{"DBParameterGroupName": "pg"}],
    }
    fake = FakeAws(db=weak_db, force_ssl=None, detectors=(), flow_logs=(("s-1", "ACTIVE"),))
    facts = read_aws(fake)
    assert facts["db_public"].value is True
    assert facts["db_encrypted_at_rest"].value is False
    assert facts["db_backups"].value is False
    # mysql 은 require_secure_transport 를 찾는데 가짜 그룹에는 없다 → 판단 못 함
    assert facts["db_tls_required"].failure == "not_found"
    assert facts["threat_detection"].value is False
    assert "디텍터 없음" in facts["threat_detection"].evidence
    assert facts["flow_logs"].value is False


def test_aws_subnet_flow_logs_count_when_every_cluster_subnet_is_covered():
    fake = FakeAws(flow_logs=(("s-1", "ACTIVE"), ("s-2", "ACTIVE")))
    assert read_aws(fake)["flow_logs"].value is True


def test_aws_one_denied_call_does_not_hide_other_fields():
    fake = FakeAws(fail={"list_detectors": aws_error("AccessDeniedException", "ListDetectors")})
    facts = read_aws(fake)
    assert facts["threat_detection"].failure == "denied"
    assert facts["threat_detection"].evidence == "guardduty:ListDetectors, guardduty:GetDetector"
    assert facts["db_public"].value is False
    assert facts["flow_logs"].value is True


def test_aws_denied_db_listing_fails_every_db_fact():
    fake = FakeAws(fail={"describe_db_instances": aws_error("AccessDenied")})
    facts = read_aws(fake)
    for fact in ("db_public", "db_tls_required", "db_encrypted_at_rest", "db_backups"):
        assert facts[fact].failure == "denied"
    assert facts["threat_detection"].value is True


def test_aws_parameter_permission_is_separate_from_instance_permission():
    fake = FakeAws(fail={"describe_db_parameters": aws_error("AccessDenied")})
    facts = read_aws(fake)
    assert facts["db_tls_required"].failure == "denied"
    assert facts["db_encrypted_at_rest"].value is True


# GCP 리더 (가짜 HTTP)


SQL = "https://sqladmin.googleapis.com/v1/projects/ghost-project-513812/instances/oneaction-dev-review"
SCC = (
    "https://securitycentermanagement.googleapis.com/v1/projects/ghost-project-513812/"
    "locations/global/securityCenterServices/event-threat-detection"
)
GKE = (
    "https://container.googleapis.com/v1/projects/ghost-project-513812/locations/"
    "asia-northeast3/clusters/oneaction-dev"
)
SUBNET_PATH = "projects/ghost-project-513812/regions/asia-northeast3/subnetworks/gke-subnet"
SUBNET = f"https://compute.googleapis.com/compute/v1/{SUBNET_PATH}"


def sql_instance(**ip):
    return {
        "name": "oneaction-dev-review",
        "settings": {
            "ipConfiguration": {"ipv4Enabled": False, "sslMode": "ENCRYPTED_ONLY", **ip},
            "backupConfiguration": {"enabled": True},
        },
    }


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url):
        body = self.routes.get(url, 404)
        request = httpx.Request("GET", url)
        if isinstance(body, int):
            return httpx.Response(body, request=request)
        return httpx.Response(200, json=body, request=request)


def read_gcp(routes):
    return GcpSecurityReader(http=lambda: FakeHttp(routes)).read(GCP).facts


HEALTHY = {
    SQL: sql_instance(),
    SCC: {"effectiveEnablementState": "ENABLED"},
    GKE: {"networkConfig": {"subnetwork": SUBNET_PATH}},
    SUBNET: {"logConfig": {"enable": True}},
}


def test_gcp_reader_maps_healthy_project():
    facts = read_gcp(HEALTHY)
    assert {k: v.value for k, v in facts.items()} == {
        "db_public": False,
        "db_tls_required": True,
        "db_encrypted_at_rest": True,
        "db_backups": True,
        "threat_detection": True,
        "flow_logs": True,
    }
    assert "Google 관리 키" in facts["db_encrypted_at_rest"].evidence


def test_gcp_reader_maps_weak_project_and_cmek_is_information_only():
    weak = sql_instance(
        ipv4Enabled=True,
        sslMode="ALLOW_UNENCRYPTED_AND_ENCRYPTED",
        authorizedNetworks=[{"value": "0.0.0.0/0"}],
    )
    weak["settings"]["backupConfiguration"]["enabled"] = False
    weak["diskEncryptionConfiguration"] = {"kmsKeyName": "projects/p/keys/k"}
    routes = {
        **HEALTHY,
        SQL: weak,
        SCC: {"effectiveEnablementState": "DISABLED"},
        SUBNET: {"logConfig": {"enable": False}},
    }
    facts = read_gcp(routes)
    assert facts["db_public"].value is True
    assert "0.0.0.0/0 포함" in facts["db_public"].evidence
    assert facts["db_tls_required"].value is False
    assert facts["db_encrypted_at_rest"].value is True
    assert "CMEK projects/p/keys/k" in facts["db_encrypted_at_rest"].evidence
    assert facts["db_backups"].value is False
    assert facts["threat_detection"].value is False
    assert facts["flow_logs"].value is False


def test_gcp_scc_403_is_denied_and_other_fields_still_read():
    facts = read_gcp({**HEALTHY, SCC: 403})
    assert facts["threat_detection"].failure == "denied"
    assert "HTTP 403" in facts["threat_detection"].evidence
    assert facts["flow_logs"].value is True
    got = result(with_declared(GCP, threat_detection=None), "DET-01", cloud("gcp", **facts))
    assert got.status == "unknown"
    assert "권한 없음" in got.fallback_reason


def test_gcp_service_disabled_403_is_not_reported_as_permission():
    class Disabled(FakeHttp):
        def get(self, url):
            if url != SCC:
                return super().get(url)
            body = {
                "error": {
                    "code": 403,
                    "details": [{"reason": "SERVICE_DISABLED", "domain": "googleapis.com"}],
                }
            }
            return httpx.Response(403, json=body, request=httpx.Request("GET", url))

    facts = GcpSecurityReader(http=lambda: Disabled(HEALTHY)).read(GCP).facts
    assert facts["threat_detection"].failure == "api_disabled"
    assert facts["threat_detection"].reason().startswith("API 비활성")


def test_gcp_missing_project_is_unconfigured():
    env = GCP.model_copy(
        update={"cloud_scope": GCP.cloud_scope.model_copy(update={"project": None})}
    )
    facts = GcpSecurityReader(http=lambda: FakeHttp({})).read(env).facts
    assert all(o.failure == "unconfigured" for o in facts.values())


# 시뮬레이터와 서비스


def test_simulated_posture_shows_observed_and_fallback_sources():
    posture = SecurityService(SimulatedFleet(DEFAULT_FLEET), POLICY).get_posture()
    controls = {c.id: c for c in posture.controls}
    assert controls["DATA-01"].results["aws"].source == "observed"
    assert controls["DATA-01"].results["onprem"].source == "declared"
    det04 = controls["DET-04"].results["aws"]
    assert (det04.source, det04.status) == ("declared", "pass")
    assert det04.fallback_reason.startswith("권한 없음")
    det01 = controls["DET-01"].results["gcp"]
    assert (det01.source, det01.status) == ("declared", "fail")


def test_journal_fleet_attaches_stored_cloud_facts_and_drops_invalid_rows():
    from controlplane.integrations.live import JournalFleet

    stored = cloud(flow_logs=Observation.seen(True, "켜짐")).model_dump(mode="json")

    class Journal:
        def get_facts(self, env_id):
            return None

        def get_cloud_security(self, env_id):
            return {"aws": {"facts": stored}, "gcp": {"facts": {"bad": 1}}}.get(env_id)

    config = FleetConfig(mode="live", environments=[ONPREM, AWS, GCP], migrations=[])
    snapshots = {s.config.id: s for s in JournalFleet(config, Journal()).list_environments()}
    assert snapshots["aws"].cloud.facts["flow_logs"].value is True
    assert snapshots["gcp"].cloud is None
    assert snapshots["onprem"].cloud is None
