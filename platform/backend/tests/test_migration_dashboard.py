from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from controlplane.domain.fleet import (
    ClusterFacts,
    EnvironmentSnapshot,
    FleetConfig,
    WorkerSample,
)
from controlplane.domain.migration import Stage, StageProgress, progress_of, stage_index, status_of
from controlplane.integrations.defaults import AWS, DEFAULT_FLEET, ONPREM
from controlplane.integrations.kube import is_default_deny, wildcard_roles, workload_of
from controlplane.integrations.simulator import SimulatedFleet
from controlplane.main import create_app
from controlplane.services.migrations import (
    MigrationService,
    downsample,
    latency_peak,
    write_pause_seconds,
)
from controlplane.services.security import SecurityService

START = datetime(2026, 11, 5, tzinfo=UTC)
TOKEN = "ingest-test-token-0123456789-abcdef"


def sample(offset: int, state: str = "RUNNING", latency_max: float = 0.04) -> WorkerSample:
    return WorkerSample(
        at=START + timedelta(seconds=offset),
        state=state,
        target="onprem",
        phase="onprem_live",
        queue_length=0,
        committed_by_target={"onprem": offset},
        counters={},
        latency_p95=0.02,
        latency_max=latency_max,
    )


def test_stage_progress_marks_failed_stage_and_done_stages():
    assert status_of(Stage.READY) == "waiting_approval"
    assert status_of(Stage.NEEDS_RECOVERY) == "needs_recovery"
    assert stage_index(Stage.ABORTED, Stage.FENCING) == 4
    assert progress_of(4, 4, failed=True, finished=False) == StageProgress.FAILED
    assert progress_of(3, 4, failed=True, finished=False) == StageProgress.DONE


def test_write_pause_is_longest_contiguous_paused_window():
    samples = [
        sample(0),
        sample(5, "PAUSED"),
        sample(10, "PAUSED"),
        sample(15, "SWITCHING"),
        sample(20),
    ]
    samples += [sample(25, "PAUSED"), sample(30)]
    assert write_pause_seconds(samples) == 15


def test_latency_peak_ignores_maximum_inherited_from_before_the_window():
    assert latency_peak([sample(0, latency_max=29.0), sample(5, latency_max=29.0)]) is None
    assert latency_peak([sample(0), sample(5, latency_max=3.5), sample(10, latency_max=1.0)]) == 3.5


def test_downsample_keeps_every_paused_sample_and_the_last_point():
    samples = [sample(i, "PAUSED" if 500 <= i < 503 else "RUNNING") for i in range(2000)]
    kept = downsample(samples)
    assert len(kept) < len(samples)
    assert [s for s in kept if s.state == "PAUSED"] == samples[500:503]
    assert kept[-1] is samples[-1]


@pytest.fixture
def simulated():
    clock = SimpleNamespace(now=START.timestamp() + 3600)
    return SimulatedFleet(DEFAULT_FLEET, anchor=START, clock=lambda: clock.now), clock


def test_simulated_forward_migration_proves_zero_failed_writes(simulated):
    fleet, _ = simulated
    service = MigrationService(fleet, now=lambda: START + timedelta(hours=1))
    forward = service.get_detail("forward_001")
    assert forward.summary.status == "completed"
    assert forward.summary.downtime.failed_requests == 0
    assert forward.summary.downtime.write_pause_seconds == pytest.approx(29, abs=5)
    assert forward.verify.post.passed and forward.verify.post.counts.S_minus_T == 0
    assert forward.stages[7].point_of_no_return and forward.stages[7].status == StageProgress.DONE
    assert any(gate.outcome == "hold" for gate in forward.gates)


def test_simulated_return_waits_for_approval_without_inherited_latency(simulated):
    fleet, _ = simulated
    summary = next(
        s
        for s in MigrationService(fleet, now=lambda: START + timedelta(hours=1)).list_summaries()
        if s.job_id == "return_001"
    )
    assert summary.status == "waiting_approval"
    assert summary.downtime.max_latency_seconds is None
    assert summary.writes.committed_target == 0


def test_simulated_series_shows_writes_moving_from_source_to_target(simulated):
    fleet, clock = simulated
    clock.now = START.timestamp() + 1800
    service = MigrationService(fleet, now=lambda: datetime.fromtimestamp(clock.now, UTC))
    points = service.get_series("forward_001", 60).points
    before = [p for p in points if p.stage == Stage.CATCHING_UP]
    after = [p for p in points if p.stage == Stage.VERIFYING]
    assert before and all(p.write_rate_target == 0 for p in before)
    assert any(p.write_rate_target > 0 for p in after) and all(
        p.write_rate_source == 0 for p in after[1:]
    )
    assert max(p.queue_length for p in points) == 29


def test_security_parity_flags_controls_that_regress_on_target():
    weak = AWS.model_copy(
        update={"declared": AWS.declared.model_copy(update={"db_tls_required": False})}
    )
    config = FleetConfig(environments=[ONPREM, weak], migrations=[])
    fleet = SimpleNamespace(
        mode="simulated",
        list_environments=lambda: [
            EnvironmentSnapshot(ONPREM, ClusterFacts(observed_at=None, connected=False)),
            EnvironmentSnapshot(weak, ClusterFacts(observed_at=None, connected=False)),
        ],
    )
    posture = SecurityService(fleet).get_posture()
    assert "DATA-01" in posture.parity.regressions
    assert config.environment("aws").declared.db_tls_required is False
    control = next(c for c in posture.controls if c.id == "NET-01")
    assert control.results["onprem"].status == "unknown"


def test_kube_helpers_read_policy_workload_and_rbac_shapes():
    deny = SimpleNamespace(
        spec=SimpleNamespace(
            pod_selector=SimpleNamespace(match_labels=None, match_expressions=None),
            policy_types=["Ingress"],
            ingress=None,
        )
    )
    allow = SimpleNamespace(
        spec=SimpleNamespace(
            pod_selector=SimpleNamespace(match_labels={"app": "x"}, match_expressions=None),
            policy_types=["Ingress"],
            ingress=[{}],
        )
    )
    assert is_default_deny(deny) and not is_default_deny(allow)
    caps = SimpleNamespace(drop=["ALL"])
    container = SimpleNamespace(
        image="img@sha256:" + "a" * 64,
        security_context=SimpleNamespace(
            run_as_non_root=None,
            read_only_root_filesystem=True,
            privileged=False,
            capabilities=caps,
        ),
    )
    pod = SimpleNamespace(
        metadata=SimpleNamespace(namespace="hackathon", name="api-1", labels={"app": "review-api"}),
        spec=SimpleNamespace(
            security_context=SimpleNamespace(
                run_as_non_root=True, read_only_root_filesystem=None, privileged=None
            ),
            containers=[container],
        ),
    )
    workload = workload_of(pod)
    assert workload.name == "review-api" and workload.run_as_non_root and workload.read_only_root
    role = SimpleNamespace(
        metadata=SimpleNamespace(namespace="hackathon", name="full"),
        rules=[SimpleNamespace(verbs=["*"], resources=["*"])],
    )
    scoped = SimpleNamespace(
        metadata=SimpleNamespace(namespace="hackathon", name="read"),
        rules=[SimpleNamespace(verbs=["get"], resources=["pods"])],
    )
    assert wildcard_roles([role, scoped]) == ["hackathon/full"]


def test_dashboard_api_serves_simulated_fleet_without_execution_config(monkeypatch):
    monkeypatch.delenv("ONEPORT_CONFIG", raising=False)
    monkeypatch.delenv("ONEPORT_RUNTIME_CONFIG", raising=False)
    with TestClient(create_app()) as client:
        fleet = client.get("/api/v1/fleet").json()
        assert fleet["source"] == "simulated" and len(fleet["migrations"]) == 2
        assert client.get("/api/v1/migrations/forward_001/series?minutes=1441").status_code == 422
        assert client.get("/api/v1/migrations/unknown_job").status_code == 404
        assert client.get("/api/v1/security/posture").json()["standards"]
        assert client.post("/api/v1/ingest/migrations/forward_001", json={}).status_code == 503


def test_ingested_cutover_report_drives_live_dashboard(store, monkeypatch, tmp_path):
    from controlplane.providers.registry import ProviderRegistry

    config = DEFAULT_FLEET.model_copy(update={"mode": "live"})
    path = tmp_path / "fleet.json"
    path.write_text(config.model_dump_json())
    monkeypatch.setenv("ONEPORT_FLEET_CONFIG", str(path))
    monkeypatch.setenv("ONEPORT_INGEST_TOKEN", TOKEN)
    app = create_app(registry=ProviderRegistry(), store=store, auth_mode="demo")
    report = {
        "cutover": {
            "job_id": "forward_001",
            "stage": "ABORTED",
            "updated_at": "2026-11-05T01:00:00Z",
            "checkpoint": {"failed_stage": "DRAINING", "error": "offset 미정지"},
        },
        "policy_decisions": [
            {
                "decision_id": "d1",
                "job_id": "forward_001",
                "stage": "pre_cutover",
                "outcome": "deny",
                "reason_codes": ["APP-01-TARGET-DB-UNREACHABLE"],
                "policy_version": "migration-policy-v1",
                "evaluated_at": "2026-11-05T00:59:00Z",
            }
        ],
        "verify": {
            "mode": "pre",
            "report": {
                "passed": False,
                "counts": {"B_minus_T": 3, "T_minus_B": 0, "field_mismatch": 0, "S_minus_T": 3},
                "source_count": 10,
                "target_count": 7,
                "committed_count": 10,
            },
        },
    }
    with TestClient(app) as client:
        path_url = "/api/v1/ingest/migrations/forward_001"
        assert client.post(path_url, json=report).status_code == 401
        headers = {"Authorization": "Bearer " + TOKEN}
        assert client.post(path_url, json=report, headers=headers).status_code == 202
        mismatched = {"cutover": {**report["cutover"], "job_id": "return_001"}}
        assert client.post(path_url, json=mismatched, headers=headers).status_code == 422
        detail = client.get("/api/v1/migrations/forward_001").json()
    assert detail["summary"]["status"] == "aborted"
    assert detail["summary"]["stage_index"] == 5
    assert [s["status"] for s in detail["stages"]][5] == "failed"
    assert detail["gates"][0]["outcome"] == "deny"
    assert detail["verify"]["pre"]["counts"]["B_minus_T"] == 3


def test_component_status_and_issues_follow_workload_readiness():
    from controlplane.domain.topology import component_view
    from controlplane.services.fleet import environment_issues

    component = ONPREM.components[0]
    degraded = ClusterFacts(
        observed_at=START, connected=True, workload_ready={component.workload: (1, 2)}
    )
    view = component_view(component, degraded)
    assert view.status == "degraded" and "1/2" in view.detail
    down = ClusterFacts(
        observed_at=START, connected=True, workload_ready={component.workload: (0, 2)}
    )
    issues = environment_issues(
        EnvironmentSnapshot(ONPREM, down), [component_view(component, down)]
    )
    assert issues[0].severity == "critical"
    blind = ClusterFacts(observed_at=START, connected=True)
    views = [component_view(c, blind) for c in AWS.components]
    assert [i.title for i in environment_issues(EnvironmentSnapshot(AWS, blind), views)] == [
        "클러스터 구성 상태를 읽지 못했어요"
    ]
    offline = environment_issues(
        EnvironmentSnapshot(AWS, ClusterFacts(observed_at=None, connected=False)), []
    )
    assert offline[0].severity == "critical"
