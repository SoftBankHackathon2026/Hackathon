from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from controlplane.core.errors import OperationNotFound
from controlplane.domain.fleet import MigrationState, WorkerSample
from controlplane.domain.migration import (
    FAILURE_STATUS,
    FLOW,
    FLOW_STAGES,
    POINT_OF_NO_RETURN,
    MigrationStatus,
    Stage,
    progress_of,
    stage_index,
    status_of,
)
from controlplane.domain.task_plan import (
    PlanPhase,
    Signals,
    phase_status,
    plan_steps,
    route_label,
)
from controlplane.integrations.base import FleetSource
from controlplane.schemas.migrations import (
    ConnectorView,
    DowntimeEvidence,
    MigrationDetail,
    MigrationEvent,
    MigrationSeries,
    MigrationSummary,
    PlanPhaseView,
    PlanRoute,
    PlanStepView,
    SeriesPoint,
    StageView,
    TaskPlan,
    VerifyPair,
    WriteCounts,
)

MIGRATION_NOT_FOUND = "등록된 마이그레이션 작업이 없어요."
PHASE_TITLES = {
    PlanPhase.ENVIRONMENT: "환경 마이그레이션",
    PlanPhase.APPLICATION: "앱 마이그레이션",
}
PAUSED_STATES = frozenset({"PAUSED", "SWITCHING", "ERROR"})
MAX_SERIES_POINTS = 720
DEFAULT_WINDOW_MINUTES = 120
MAX_LIFETIME = timedelta(days=1)
# 전환이 진행 중(승인 대기 포함)이거나 복구를 기다리는 작업. 지우면 진행 상황과 복구 경로를 화면에서 잃는다.
IN_PROGRESS_STATUSES = frozenset(
    {MigrationStatus.RUNNING, MigrationStatus.WAITING_APPROVAL, MigrationStatus.NEEDS_RECOVERY}
)
CONNECTOR_RUNNING = "RUNNING"


def in_progress(status: MigrationStatus, connectors: Sequence[ConnectorView]) -> bool:
    """전환 단계가 끝나지 않았거나 CDC 커넥터가 아직 돌고 있으면 진행 중이다."""
    return status in IN_PROGRESS_STATUSES or any(
        connector.state == CONNECTOR_RUNNING for connector in connectors
    )


def write_pause_seconds(samples: Sequence[WorkerSample]) -> float | None:
    if len(samples) < 2:
        return None
    longest = current = 0.0
    for previous, sample in zip(samples, samples[1:], strict=False):
        if previous.state in PAUSED_STATES:
            current += (sample.at - previous.at).total_seconds()
            longest = max(longest, current)
        else:
            current = 0.0
    return longest


def write_counts(state: MigrationState, latest: WorkerSample | None) -> WriteCounts:
    committed = state.worker.committed_by_target if state.worker else {}
    counters = latest.counters if latest else {}
    rejected = counters.get("queue_rejected", 0)
    mismatch = counters.get("mismatch", 0)
    return WriteCounts(
        committed_source=committed.get(state.config.data_source, 0),
        committed_target=committed.get(state.config.data_target, 0),
        failed=rejected + mismatch,
        rejected=rejected,
        mismatch=mismatch,
    )


def latency_peak(samples: Sequence[WorkerSample]) -> float | None:
    maxima = [s.latency_max for s in samples if s.latency_max is not None]
    if len(maxima) < 2:
        return None
    risen = [value for value in maxima[1:] if value > maxima[0]]
    return max(risen) if risen else None


def downtime(samples: Sequence[WorkerSample], counts: WriteCounts) -> DowntimeEvidence:
    return DowntimeEvidence(
        write_pause_seconds=write_pause_seconds(samples),
        failed_requests=counts.failed,
        max_latency_seconds=latency_peak(samples),
        p95_latency_seconds=samples[-1].latency_p95 if samples else None,
    )


def verify_mismatch(state: MigrationState) -> int | None:
    report = state.verify_post or state.verify_pre
    if report is None:
        return None
    counts = report.counts
    return counts.B_minus_T + counts.T_minus_B + counts.field_mismatch + counts.S_minus_T


def _failed_stage(state: MigrationState) -> Stage | None:
    if not state.cutover:
        return None
    failed = state.cutover.checkpoint.get("failed_stage")
    return Stage(failed) if failed in FLOW_STAGES else None


def stage_views(state: MigrationState) -> list[StageView]:
    cutover = state.cutover
    stage = cutover.stage if cutover else None
    current = stage_index(stage, _failed_stage(state)) if cutover else -1
    failed = stage in FAILURE_STATUS
    finished = stage == Stage.COMPLETED
    times = dict(cutover.stage_times) if cutover else {}
    starts = [times.get(info.stage) for info in FLOW]
    views = []
    for position, info in enumerate(FLOW):
        following = next((s for s in starts[position + 1 :] if s is not None), None)
        started = starts[position]
        views.append(
            StageView(
                name=info.stage,
                title=info.title,
                description=info.description,
                status=progress_of(position, current, failed=failed, finished=finished),
                started_at=started,
                finished_at=following or (started if finished else None) if started else None,
                point_of_no_return=info.stage == POINT_OF_NO_RETURN,
                evidence=cutover.checkpoint.get(info.evidence_key, {}) if cutover else {},
            )
        )
    return views


def series_points(samples: Sequence[WorkerSample], state: MigrationState) -> list[SeriesPoint]:
    times = state.cutover.stage_times if state.cutover else []
    points = []
    previous = None
    for sample in samples:
        source = sample.committed_by_target.get(state.config.data_source, 0)
        target = sample.committed_by_target.get(state.config.data_target, 0)
        elapsed = (sample.at - previous.at).total_seconds() if previous else 0.0
        rate_source = rate_target = 0.0
        if previous and elapsed > 0:
            rate_source = (
                max(0, source - previous.committed_by_target.get(state.config.data_source, 0))
                / elapsed
            )
            rate_target = (
                max(0, target - previous.committed_by_target.get(state.config.data_target, 0))
                / elapsed
            )
        stage = next((s for s, at in reversed(times) if at <= sample.at), None)
        points.append(
            SeriesPoint(
                at=sample.at,
                committed_source=source,
                committed_target=target,
                write_rate_source=round(rate_source, 3),
                write_rate_target=round(rate_target, 3),
                queue_length=sample.queue_length,
                latency_p95=sample.latency_p95,
                latency_max=sample.latency_max,
                failed_total=sample.counters.get("queue_rejected", 0)
                + sample.counters.get("mismatch", 0),
                worker_state=sample.state,
                stage=stage,
            )
        )
        previous = sample
    return points


def downsample(samples: list[WorkerSample]) -> list[WorkerSample]:
    if len(samples) <= MAX_SERIES_POINTS:
        return samples
    stride = -(-len(samples) // MAX_SERIES_POINTS)
    keep = set(range(0, len(samples), stride)) | {len(samples) - 1}
    keep |= {i for i, sample in enumerate(samples) if sample.state in PAUSED_STATES}
    return [samples[i] for i in sorted(keep)]


GATE_STAGE_TITLES = {
    "plan": "계획",
    "pre_start": "시작 전",
    "pre_cutover": "전환 전",
    "final_verify": "최종 검증",
}
GATE_OUTCOME_TITLES = {"allow": "허용", "deny": "거부", "hold": "보류", "invalid": "입력 오류"}


def gate_title(stage: str, outcome: str) -> str:
    return f"정책 게이트, {GATE_STAGE_TITLES.get(stage, stage)} {GATE_OUTCOME_TITLES.get(outcome, outcome)}"


def migration_events(state: MigrationState) -> list[MigrationEvent]:
    titles = {info.stage: info.title for info in FLOW}
    events = [
        MigrationEvent(
            at=at,
            kind="switch" if stage == POINT_OF_NO_RETURN else "stage",
            title=f"{titles.get(stage, stage)} 시작",
            detail=str(stage),
        )
        for stage, at in (state.cutover.stage_times if state.cutover else [])
    ]
    events += [
        MigrationEvent(
            at=gate.evaluated_at,
            kind="gate" if gate.outcome == "allow" else "warning",
            title=gate_title(gate.stage, gate.outcome),
            detail=", ".join(gate.reason_codes) or "모든 규칙 통과",
        )
        for gate in state.gates
    ]
    if state.cutover and state.cutover.stage in FAILURE_STATUS:
        events.append(
            MigrationEvent(
                at=state.cutover.updated_at,
                kind="warning",
                title=f"전환 중단: {state.cutover.stage}",
                detail=str(state.cutover.checkpoint.get("error", "")),
            )
        )
    return sorted(events, key=lambda event: event.at, reverse=True)


class MigrationService:
    def __init__(
        self, source: FleetSource, now: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._source = source
        self._now = now

    @property
    def mode(self) -> str:
        return self._source.mode

    def _state(self, job_id: str) -> MigrationState:
        for state in self._source.list_migrations():
            if state.config.job_id == job_id:
                return state
        raise OperationNotFound(MIGRATION_NOT_FOUND)

    def _window(self, job_id: str, minutes: int) -> list[WorkerSample]:
        until = self._now()
        return self._source.list_samples(job_id, until - timedelta(minutes=minutes), until)

    def _lifetime(self, state: MigrationState) -> list[WorkerSample]:
        until = self._now()
        since = until - timedelta(minutes=DEFAULT_WINDOW_MINUTES)
        if state.cutover and state.cutover.stage_times:
            since = max(state.cutover.stage_times[0][1], until - MAX_LIFETIME)
        return self._source.list_samples(state.config.job_id, since, until)

    def summarize(self, state: MigrationState, samples: Sequence[WorkerSample]) -> MigrationSummary:
        stage = state.cutover.stage if state.cutover else None
        counts = write_counts(state, samples[-1] if samples else None)
        return MigrationSummary(
            job_id=state.config.job_id,
            direction=state.config.direction,
            source_env=state.config.source_env,
            target_env=state.config.target_env,
            data_source=state.config.data_source,
            data_target=state.config.data_target,
            stage=stage,
            status=status_of(stage),
            stage_index=stage_index(stage, _failed_stage(state)) if stage else 0,
            stage_count=len(FLOW),
            updated_at=state.cutover.updated_at if state.cutover else None,
            writes=counts,
            downtime=downtime(samples, counts),
            verify_mismatch=verify_mismatch(state),
        )

    def list_summaries(self) -> list[MigrationSummary]:
        return [
            self.summarize(state, self._lifetime(state)) for state in self._source.list_migrations()
        ]

    def in_progress_jobs(self, job_ids: set[str]) -> set[str]:
        busy = set()
        for state in self._source.list_migrations():
            if state.config.job_id not in job_ids:
                continue
            stage = state.cutover.stage if state.cutover else None
            if in_progress(status_of(stage), state.connectors):
                busy.add(state.config.job_id)
        return busy

    def get_detail(self, job_id: str) -> MigrationDetail:
        state = self._state(job_id)
        return MigrationDetail(
            summary=self.summarize(state, self._lifetime(state)),
            stages=stage_views(state),
            gates=sorted(state.gates, key=lambda gate: gate.evaluated_at),
            verify=VerifyPair(pre=state.verify_pre, post=state.verify_post),
            connectors=state.connectors,
            worker=state.worker,
        )

    def get_plan(self, job_id: str, mapping_status: str | None) -> TaskPlan:
        state = self._state(job_id)
        environments = {env.config.id: env for env in self._source.list_environments()}
        source = environments.get(state.config.source_env)
        target = environments.get(state.config.target_env)
        source_provider = (
            source.config.provider
            if source
            else state.config.source_provider or state.config.source_env
        )
        target_provider = (
            target.config.provider
            if target
            else state.config.target_provider or state.config.target_env
        )
        signals = Signals(
            source_connected=bool(source and source.cluster.connected),
            target_connected=bool(target and target.cluster.connected),
            mapping_status=mapping_status,
            stage=state.cutover.stage if state.cutover else None,
            failed_stage=_failed_stage(state),
            verify_passed=state.verify_post.passed if state.verify_post else None,
        )
        steps = plan_steps(source_provider, target_provider, signals)
        phases = []
        for phase in PlanPhase:
            members = [step for step in steps if step.definition.phase == phase]
            phases.append(
                PlanPhaseView(
                    id=phase,
                    title=PHASE_TITLES[phase],
                    status=phase_status(members),
                    steps=[
                        PlanStepView(
                            id=step.definition.id,
                            title=step.definition.title,
                            description=step.definition.description,
                            tool=step.tool,
                            status=step.status,
                            runnable=step.runnable,
                        )
                        for step in members
                    ],
                )
            )
        return TaskPlan(
            job_id=job_id,
            route=PlanRoute(
                source_env=state.config.source_env,
                target_env=state.config.target_env,
                source_provider=source_provider,
                target_provider=target_provider,
                label=route_label(source_provider, target_provider),
            ),
            phases=phases,
        )

    def get_series(self, job_id: str, minutes: int) -> MigrationSeries:
        state = self._state(job_id)
        samples = self._window(job_id, minutes)
        kept = downsample(samples)
        step = int((kept[1].at - kept[0].at).total_seconds()) if len(kept) > 1 else 0
        return MigrationSeries(job_id=job_id, step_seconds=step, points=series_points(kept, state))

    def list_events(self, job_id: str) -> list[MigrationEvent]:
        return migration_events(self._state(job_id))
