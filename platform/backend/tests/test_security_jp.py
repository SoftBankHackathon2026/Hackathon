from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from controlplane.core.config import SecuritySettings
from controlplane.domain.compliance_mappings import (
    ISMAP,
    MAPPINGS,
    StandardMapping,
    standard_refs,
)
from controlplane.domain.fleet import ClusterFacts, EnvironmentSnapshot, NodeFact
from controlplane.domain.security_controls import CONTROLS, STANDARDS, SecurityPolicy, evaluate
from controlplane.integrations.defaults import AWS, GCP, ONPREM
from controlplane.schemas.migrations import DataService
from controlplane.services.security import SecurityService, policy_from_settings, posture_of

NOW = datetime(2026, 11, 8, tzinfo=UTC)
POLICY = SecurityPolicy(
    japan_regions=frozenset({"ap-northeast-1", "ap-northeast-3", "asia-northeast1"}),
    k8s_min_minor="1.34",
    k8s_max_node_skew=1,
)


def snapshot(env, cluster=None):
    return EnvironmentSnapshot(env, cluster or ClusterFacts(observed_at=None, connected=False))


def check(control_id, snap, policy=POLICY):
    return evaluate(snap, policy)[control_id]


def in_region(env, region):
    return env.model_copy(update={"region": region})


def cluster(version="v1.35.4", kubelets=("v1.35.4", "v1.35.2")):
    nodes = None
    if kubelets is not None:
        nodes = [NodeFact(name=f"node-{i}", kubelet_version=v) for i, v in enumerate(kubelets)]
    return ClusterFacts(observed_at=NOW, connected=True, version=version, nodes=nodes)


# JP-01 일본 리전 데이터 저장


def test_jp01_passes_when_cluster_and_data_services_are_in_japan():
    result = check("JP-01", snapshot(in_region(AWS, "ap-northeast-1")))
    assert result.status == "pass"
    assert "ap-northeast-1" in result.detail


def test_jp01_fails_for_seoul_region_and_names_what_is_outside():
    result = check("JP-01", snapshot(AWS))
    assert result.status == "fail"
    assert "ap-northeast-2" in result.detail
    assert "RDS oneaction-review-db" in result.detail


def test_jp01_fails_when_one_data_service_sits_outside_japan():
    replica = DataService(
        kind="object_storage", name="S3 replica", managed=True, region="us-east-1"
    )
    env = in_region(GCP, "asia-northeast1")
    env = env.model_copy(update={"data_services": [*env.data_services, replica]})
    result = check("JP-01", snapshot(env))
    assert result.status == "fail"
    assert result.detail == "일본 밖 리전: S3 replica(us-east-1)"


def test_jp01_is_unknown_for_onprem_site_with_reason():
    result = check("JP-01", snapshot(ONPREM))
    assert result.status == "unknown"
    assert "seoul-local" in result.detail


# JP-09 지원되는 클러스터/노드 버전


def test_jp09_passes_within_supported_range_and_skew():
    result = check("JP-09", snapshot(GCP, cluster()))
    assert result.status == "pass"
    assert "v1.35" in result.detail


def test_jp09_fails_when_control_plane_is_below_floor():
    result = check("JP-09", snapshot(GCP, cluster(version="v1.33.5-gke.100")))
    assert result.status == "fail"
    assert "지원 하한 v1.34 미만" in result.detail


def test_jp09_fails_when_node_lags_beyond_skew_or_runs_ahead():
    lagging = check("JP-09", snapshot(GCP, cluster(kubelets=("v1.35.4", "v1.33.9+k3s1"))))
    assert lagging.status == "fail"
    assert "node-1 v1.33" in lagging.detail
    ahead = check("JP-09", snapshot(GCP, cluster(kubelets=("v1.36.0",))))
    assert ahead.status == "fail"
    assert "컨트롤플레인보다 새 버전" in ahead.detail


def test_jp09_skew_and_floor_come_from_policy():
    lenient = POLICY.model_copy(update={"k8s_min_minor": "1.30", "k8s_max_node_skew": 3})
    snap = snapshot(GCP, cluster(version="v1.32.1", kubelets=("v1.30.2",)))
    assert check("JP-09", snap).status == "fail"
    assert check("JP-09", snap, lenient).status == "pass"


@pytest.mark.parametrize(
    "facts",
    [
        ClusterFacts(observed_at=None, connected=False),
        ClusterFacts(observed_at=NOW, connected=True, version=None),
        cluster(kubelets=None),
        cluster(kubelets=("",)),
    ],
)
def test_jp09_is_unknown_without_version_evidence(facts):
    assert check("JP-09", snapshot(GCP, facts)).status == "unknown"


# not_applicable / manual


def test_db_controls_are_not_applicable_without_a_database():
    no_db = AWS.model_copy(update={"data_services": []})
    results = evaluate(snapshot(no_db), POLICY)
    for control_id in ("NET-02", "DATA-01", "DATA-02", "DATA-03"):
        assert results[control_id].status == "not_applicable"
    assert results["DATA-01"].detail == "이 환경에는 DB가 없어요."


def test_score_excludes_not_applicable_and_manual_and_counts_them():
    no_db = AWS.model_copy(update={"data_services": []})
    fleet = SimpleNamespace(mode="simulated", list_environments=lambda: [snapshot(no_db)])
    posture = SecurityService(fleet, POLICY).get_posture()
    env = posture.environments[0]
    assert env.not_applicable == 4
    assert env.manual == 0
    assert env.passed + env.failed + env.unknown + env.not_applicable == len(CONTROLS)


def test_manual_status_is_counted_apart_from_the_score():
    snap = snapshot(AWS)
    results = evaluate(snap, POLICY)
    before = posture_of(snap, results)
    results["DET-02"] = results["DET-02"].model_copy(update={"status": "manual"})
    after = posture_of(snap, results)
    assert after.manual == 1
    assert after.failed == before.failed - 1
    assert after.passed == before.passed


# 매핑 데이터


def test_standard_refs_append_mapped_clauses_after_inline_refs():
    assert standard_refs("NET-01", ("CIS-K8S 5.3.2", "NSA-K8S")) == [
        "CIS-K8S 5.3.2",
        "NSA-K8S",
        "ISMAP 13.1.3",
        "ISMS-CLOUD CLD.9.5.1",
    ]
    assert standard_refs("DET-02", ("AWS-FSBP SecurityHub.1",)) == [
        "AWS-FSBP SecurityHub.1",
        "ISMAP 18.2.3",
    ]
    assert standard_refs("JP-09") == ["ISMAP 12.6.1"]


def test_standard_refs_skip_duplicates_and_follow_swapped_mapping():
    revised = StandardMapping("ISMAP", "2027", "개정판", {"NET-01": ("8.22", "13.1.3")})
    assert standard_refs("NET-01", ("ISMAP 13.1.3",), (revised,)) == [
        "ISMAP 13.1.3",
        "ISMAP 8.22",
    ]


def test_every_mapped_control_exists_and_refs_group_by_standard_id():
    control_ids = {c.id for c in CONTROLS}
    for mapping in MAPPINGS:
        assert set(mapping.clauses) <= control_ids
        for control_id in mapping.clauses:
            assert all(r.split(" ")[0] == mapping.standard for r in mapping.refs(control_id))
    assert set(ISMAP.clauses) == control_ids


def test_posture_lists_japanese_standards_as_coverage_with_version():
    fleet = SimpleNamespace(mode="simulated", list_environments=lambda: [snapshot(AWS)])
    posture = SecurityService(fleet, POLICY).get_posture()
    standards = {s.id: s for s in posture.standards}
    assert standards["ISMAP"].kind == "coverage"
    assert standards["ISMAP"].version == "2023"
    assert standards["ISMS-CLOUD"].version == "2016"
    assert standards["CIS-K8S"].kind == "compliance"
    jp01 = next(c for c in posture.controls if c.id == "JP-01")
    assert jp01.category == "japan"
    assert jp01.standard_refs == ["ISMAP 18.1.1", "ISMS-CLOUD 18.1.1"]
    assert {s.id for s in STANDARDS} >= {"ISMAP", "ISMS-CLOUD"}


def test_policy_reads_settings_from_environment(monkeypatch):
    monkeypatch.setenv("ONEPORT_SECURITY_JAPAN_REGIONS", "ap-northeast-1, asia-northeast2")
    monkeypatch.setenv("ONEPORT_SECURITY_K8S_MIN_MINOR", "1.35")
    monkeypatch.setenv("ONEPORT_SECURITY_K8S_MAX_NODE_SKEW", "2")
    policy = policy_from_settings(SecuritySettings())
    assert policy.japan_regions == {"ap-northeast-1", "asia-northeast2"}
    assert policy.k8s_min_minor == "1.35"
    assert policy.k8s_max_node_skew == 2
