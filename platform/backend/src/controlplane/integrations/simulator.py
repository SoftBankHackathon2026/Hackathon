import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from controlplane.domain.fleet import (
    ClusterFacts,
    CutoverRecord,
    EnvironmentConfig,
    EnvironmentSnapshot,
    FleetConfig,
    MigrationConfig,
    MigrationState,
    WorkerSample,
    Workload,
)
from controlplane.domain.migration import Stage
from controlplane.schemas.migrations import (
    AppState,
    ConnectorView,
    GateDecision,
    LatencyView,
    VerifyCounts,
    VerifyReport,
    WorkerView,
)

SEED_ROWS = 1000
WRITE_RATE = 1.0
DRAIN_RATE = 10.0
BASE_P95 = 0.018
BASE_MAX = 0.042
HISTORY_SECONDS = 3600
FORWARD_STAGES: tuple[tuple[Stage, int], ...] = (
    (Stage.PRECHECK, 0),
    (Stage.COPYING, 60),
    (Stage.CATCHING_UP, 600),
    (Stage.READY, 900),
    (Stage.FENCING, 1500),
    (Stage.DRAINING, 1506),
    (Stage.SYNC_CONFIRMED, 1518),
    (Stage.SWITCHING, 1526),
    (Stage.VERIFYING, 1529),
    (Stage.COMPLETED, 1700),
)
RETURN_STAGES: tuple[tuple[Stage, int], ...] = (
    (Stage.PRECHECK, 2400),
    (Stage.COPYING, 2440),
    (Stage.CATCHING_UP, 2900),
    (Stage.READY, 3100),
)
PAUSE_START = 1500
SWITCH_START = 1526
RESUME_AT = 1529
CONNECTORS_DELETED_AT = 1526


@dataclass(frozen=True)
class WorkerModel:
    source: str
    target: str

    def backlog_at_resume(self) -> int:
        return RESUME_AT - PAUSE_START

    def drained(self, offset: float) -> int:
        if offset < RESUME_AT:
            return 0
        return min(self.backlog_at_resume(), int((offset - RESUME_AT) * DRAIN_RATE))

    def committed(self, offset: float) -> dict[str, int]:
        on_source = SEED_ROWS + int(min(max(offset, 0), PAUSE_START) * WRITE_RATE)
        on_target = self.drained(offset) + int(max(0.0, offset - RESUME_AT) * WRITE_RATE)
        return {self.source: on_source, self.target: on_target}

    def queue(self, offset: float) -> int:
        if PAUSE_START <= offset < RESUME_AT:
            return int(offset - PAUSE_START) + 1
        return max(0, self.backlog_at_resume() - self.drained(offset))

    def state(self, offset: float) -> str:
        if PAUSE_START <= offset < SWITCH_START:
            return "PAUSED"
        if SWITCH_START <= offset < RESUME_AT:
            return "SWITCHING"
        return "RUNNING"

    def target_at(self, offset: float) -> tuple[str, str]:
        if offset >= RESUME_AT:
            return self.target, f"{self.target}_live"
        return self.source, f"{self.source}_live"

    def latency(self, offset: float) -> tuple[float, float]:
        jitter = 0.004 * math.sin(offset / 37.0)
        if offset >= RESUME_AT + self.backlog_at_resume() / DRAIN_RATE:
            return BASE_P95 + jitter, float(self.backlog_at_resume()) + 0.21
        return BASE_P95 + jitter, BASE_MAX

    def sample(self, at: datetime, offset: float) -> WorkerSample:
        target, phase = self.target_at(offset)
        committed = self.committed(offset)
        p95, worst = self.latency(offset)
        generated = SEED_ROWS + int(max(offset, 0) * WRITE_RATE)
        return WorkerSample(
            at=at,
            state=self.state(offset),
            target=target,
            phase=phase,
            queue_length=self.queue(offset),
            committed_by_target=committed,
            counters={
                "generated": generated,
                "committed_new": sum(committed.values()),
                "queue_rejected": 0,
                "mismatch": 0,
                "send_errors": 0,
            },
            latency_p95=round(p95, 4),
            latency_max=round(worst, 4),
            latency_p50=round(p95 * 0.55, 4),
        )


def _gate(job: str, stage: str, outcome: str, at: datetime, reasons: list[str]) -> GateDecision:
    return GateDecision(
        decision_id=f"{job}-{stage}-{int(at.timestamp())}",
        stage=stage,
        outcome=outcome,
        reason_codes=reasons,
        policy_version="migration-policy-v1",
        evaluated_at=at,
    )


def _verify(source: int, target: int, committed: int) -> VerifyReport:
    return VerifyReport(
        passed=True,
        counts=VerifyCounts(B_minus_T=0, T_minus_B=0, field_mismatch=0, S_minus_T=0),
        source_count=source,
        target_count=target,
        committed_count=committed,
    )


def _connectors(job: str, running: bool) -> list[ConnectorView]:
    if not running:
        return []
    return [
        ConnectorView(
            name=f"{job}-source", role="source", state="RUNNING", tasks_running=1, tasks_total=1
        ),
        ConnectorView(
            name=f"{job}-sink", role="sink", state="RUNNING", tasks_running=1, tasks_total=1
        ),
    ]


FORWARD_EVIDENCE = {
    "precheck": {"wal_level": "logical", "source_app_insert": True, "target_app_insert": False},
    "copying": {"source_seed": SEED_ROWS, "target_seed": SEED_ROWS},
    "catching_up": {"live_rows_after_snapshot": 100, "latest_rows_on_target": 10},
    "approval": {"id": "approve-forward-001"},
    "fenced": {"rejected": True, "sessions_terminated": 1},
    "drained": {"offset_stable_seconds": 10, "last_request_on_target": True},
    "verify_pre": {"passed": True},
    "switched": {"target": "aws", "phase": "aws_live", "database": "migration_demo"},
    "post_switch": {"rows_after_switch": 100, "source_still_fenced": True},
    "verify_final": {"passed": True, "source_slot_dropped": True},
}
RETURN_EVIDENCE = {
    "precheck": {"wal_level": "logical", "source_app_insert": True, "target_app_insert": False},
    "copying": {"source_seed": SEED_ROWS, "target_seed": SEED_ROWS},
    "catching_up": {"live_rows_after_snapshot": 100, "latest_rows_on_target": 10},
}


class SimulatedFleet:
    mode = "simulated"

    def __init__(
        self, config: FleetConfig, *, anchor: datetime | None = None, clock=time.time
    ) -> None:
        self._config = config
        self._clock = clock
        now = datetime.fromtimestamp(clock(), UTC)
        self._anchor = anchor or now - timedelta(seconds=HISTORY_SECONDS)

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), UTC)

    def _offset(self, at: datetime) -> float:
        return (at - self._anchor).total_seconds()

    def _at(self, offset: int) -> datetime:
        return self._anchor + timedelta(seconds=offset)

    def _worker(self, job: MigrationConfig) -> WorkerModel:
        forward = self._config.migrations[0]
        return WorkerModel(source=forward.data_source, target=forward.data_target)

    def _timeline(self, job: MigrationConfig) -> tuple[tuple[Stage, int], ...]:
        return FORWARD_STAGES if job.direction == "forward" else RETURN_STAGES

    def _reached(self, job: MigrationConfig, now: float) -> list[tuple[Stage, int]]:
        return [(stage, offset) for stage, offset in self._timeline(job) if offset <= now]

    def _cutover(self, job: MigrationConfig, now: float) -> CutoverRecord | None:
        reached = self._reached(job, now)
        if not reached:
            return None
        evidence = FORWARD_EVIDENCE if job.direction == "forward" else RETURN_EVIDENCE
        keys = list(evidence)[: max(0, len(reached) - 1)]
        if job.direction == "forward" and reached[-1][0] == Stage.COMPLETED:
            keys = list(evidence)
        stage, offset = reached[-1]
        return CutoverRecord(
            stage=stage,
            checkpoint={key: evidence[key] for key in keys},
            updated_at=self._at(offset),
            stage_times=[(s, self._at(o)) for s, o in reached],
        )

    def _gates(self, job: MigrationConfig, now: float) -> list[GateDecision]:
        base = 0 if job.direction == "forward" else 2400
        plan = [
            ("plan", "allow", base - 30, []),
            ("pre_start", "allow", base + 50, []),
        ]
        if job.direction == "forward":
            plan += [
                ("pre_cutover", "hold", 1480, ["EVIDENCE-STALE"]),
                ("pre_cutover", "allow", 1495, []),
                ("final_verify", "allow", 1695, []),
            ]
        return [
            _gate(job.job_id, stage, outcome, self._at(offset), reasons)
            for stage, outcome, offset, reasons in plan
            if offset <= now
        ]

    def _verify_reports(
        self, job: MigrationConfig, now: float
    ) -> tuple[VerifyReport | None, VerifyReport | None]:
        if job.direction != "forward":
            return None, None
        # 검증은 이 작업의 데이터 이름으로 센다. 첫 작업과 이름이 다른 등록 작업(예: 온프렘 → GCP)도 읽힌다.
        worker = WorkerModel(source=job.data_source, target=job.data_target)
        pre_at, post_at = 1518, 1695
        pre = None
        post = None
        if now >= pre_at:
            copied = worker.committed(pre_at)[job.data_source]
            pre = _verify(copied, copied, copied)
        if now >= post_at:
            counts = worker.committed(post_at)
            copied = counts[job.data_source]
            post = _verify(
                copied, copied + counts[job.data_target], copied + counts[job.data_target]
            )
        return pre, post

    def _connectors_running(self, job: MigrationConfig, now: float) -> bool:
        if job.direction == "forward":
            return 60 <= now < CONNECTORS_DELETED_AT
        return now >= 2440

    def _worker_view(self, job: MigrationConfig, now: float) -> WorkerView:
        sample = self._worker(job).sample(self._now(), now)
        return WorkerView(
            state=sample.state,
            target=sample.target,
            phase=sample.phase,
            queue_length=sample.queue_length,
            rate_per_second=WRITE_RATE,
            latency=LatencyView(
                p50=sample.latency_p50, p95=sample.latency_p95, max=sample.latency_max
            ),
            committed_by_target=sample.committed_by_target,
        )

    def list_migrations(self) -> list[MigrationState]:
        now = self._offset(self._now())
        states = []
        for job in self._config.migrations:
            pre, post = self._verify_reports(job, now)
            states.append(
                MigrationState(
                    config=job,
                    cutover=self._cutover(job, now),
                    gates=self._gates(job, now),
                    verify_pre=pre,
                    verify_post=post,
                    connectors=_connectors(job.job_id, self._connectors_running(job, now)),
                    worker=self._worker_view(job, now),
                )
            )
        return states

    def list_samples(self, job_id: str, since: datetime, until: datetime) -> list[WorkerSample]:
        job = self._config.migration(job_id)
        if job is None:
            return []
        worker = self._worker(job)
        step = self._config.sample_seconds
        start = max(since, self._anchor)
        count = int((until - start).total_seconds() // step) + 1
        return [
            worker.sample(start + timedelta(seconds=i * step), self._offset(start) + i * step)
            for i in range(max(0, count))
        ]

    def list_environments(self) -> list[EnvironmentSnapshot]:
        observed = self._now()
        return [
            EnvironmentSnapshot(config=env, cluster=_simulated_cluster(env, observed))
            for env in self._config.environments
        ]


SIMULATED_REPLICAS = {"cloudflared": 2, "kafka": 2}
HARDENED = dict(
    run_as_non_root=True, read_only_root=True, drops_all_capabilities=True, privileged=False
)
ONPREM_WORKLOADS = [
    Workload(
        "hackathon",
        "review-api",
        ["ghcr.io/crystal-sbhackathon2026/review-api:3f874c80b4edb4b42aece883d0727422a7ccff47"],
        **HARDENED,
    ),
    Workload(
        "hackathon",
        "review-worker",
        ["ghcr.io/crystal-sbhackathon2026/review-worker:3f874c80b4edb4b42aece883d0727422a7ccff47"],
        **HARDENED,
    ),
    Workload(
        "hackathon",
        "postgres",
        ["postgres:16.15-alpine"],
        run_as_non_root=True,
        read_only_root=False,
        drops_all_capabilities=True,
        privileged=False,
    ),
    Workload(
        "hackathon",
        "kafka-broker",
        ["apache/kafka:3.9.1"],
        run_as_non_root=True,
        read_only_root=False,
        drops_all_capabilities=True,
        privileged=False,
    ),
]


def _apps(env: EnvironmentConfig) -> list[AppState]:
    names = [
        "review-service",
        "sample-app",
        "migration-policy",
        "migration-cdc",
        "db-insert-worker",
    ]
    if env.provider in ("aws", "gcp"):
        # 실제 Argo CD 앱 이름은 환경 접미사가 붙는다 (review-service-aws 등). 구성 요소의 argo_app 과 맞춘다.
        names = [c.argo_app for c in env.components if c.argo_app]
    return [
        AppState(name=name, namespace=env.app_namespace, sync="Synced", health="Healthy")
        if env.provider == "onprem"
        else AppState(name=name, namespace=env.app_namespace, sync="Unknown", health="Unknown")
        for name in names
    ]


def _simulated_cluster(env: EnvironmentConfig, observed: datetime) -> ClusterFacts:
    if env.provider == "onprem":
        return ClusterFacts(
            observed_at=observed,
            connected=True,
            namespace_labels={"hackathon": {"environment": "hackathon"}},
            default_deny_namespaces={"hackathon"},
            network_policy_count={"hackathon": 6},
            workloads=ONPREM_WORKLOADS,
            wildcard_bindings=[
                "hackathon/hackathon-full-access",
                "hackathon/argocd-hackathon-deployer",
            ],
            argocd_admin_enabled=False,
            argocd_anonymous_enabled=False,
            external_secrets=0,
            resource_quota_namespaces={"hackathon"},
            apps=_apps(env),
            workload_ready={
                c.workload: (SIMULATED_REPLICAS.get(c.id, 1),) * 2
                for c in env.components
                if c.workload
            },
        )
    return ClusterFacts(
        observed_at=observed,
        connected=True,
        external_secrets=1,
        apps=_apps(env),
    )
