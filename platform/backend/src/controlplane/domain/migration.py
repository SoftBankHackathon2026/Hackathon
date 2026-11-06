from dataclasses import dataclass
from enum import StrEnum


class Stage(StrEnum):
    PRECHECK = "PRECHECK"
    COPYING = "COPYING"
    CATCHING_UP = "CATCHING_UP"
    READY = "READY"
    FENCING = "FENCING"
    DRAINING = "DRAINING"
    SYNC_CONFIRMED = "SYNC_CONFIRMED"
    SWITCHING = "SWITCHING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
    NEEDS_RECOVERY = "NEEDS_RECOVERY"
    ROLLED_BACK = "ROLLED_BACK"


class MigrationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    ABORTED = "aborted"
    NEEDS_RECOVERY = "needs_recovery"
    ROLLED_BACK = "rolled_back"


class Direction(StrEnum):
    FORWARD = "forward"
    RETURN = "return"


class StageProgress(StrEnum):
    DONE = "done"
    CURRENT = "current"
    PENDING = "pending"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class StageInfo:
    stage: Stage
    title: str
    description: str
    evidence_key: str


FLOW: tuple[StageInfo, ...] = (
    StageInfo(
        Stage.PRECHECK,
        "사전 점검",
        "양쪽 테이블/권한/wal_level/CDC 계정/워커 대상 확인",
        "precheck",
    ),
    StageInfo(
        Stage.COPYING,
        "초기 복사",
        "publication/slot 준비, Source/Sink 커넥터 등록, seed 복사",
        "copying",
    ),
    StageInfo(
        Stage.CATCHING_UP,
        "변경분 추적",
        "스냅샷 이후 실시간 쓰기가 대상에 반영되는지 확인",
        "catching_up",
    ),
    StageInfo(
        Stage.READY, "전환 승인 대기", "되돌리기 어려운 쓰기 차단 전에 사람이 승인", "approval"
    ),
    StageInfo(
        Stage.FENCING,
        "원본 쓰기 차단",
        "워커 일시정지/drain, 원본 INSERT 회수, 시험 INSERT 거절 확인",
        "fenced",
    ),
    StageInfo(
        Stage.DRAINING,
        "최종 반영",
        "마지막 요청이 대상에 도착하고 sink offset이 멈출 때까지 대기",
        "drained",
    ),
    StageInfo(
        Stage.SYNC_CONFIRMED,
        "동기화 확정",
        "verify(pre) 통과, 커넥터 증거 저장 후 CDC 종료",
        "verify_pre",
    ),
    StageInfo(Stage.SWITCHING, "쓰기 전환", "대상 INSERT 허용, 워커 대상 변경 후 재개", "switched"),
    StageInfo(
        Stage.VERIFYING,
        "전환 후 검증",
        "대상 신규 커밋, 원본 차단 유지, verify(post) 통과",
        "post_switch",
    ),
    StageInfo(Stage.COMPLETED, "완료", "승인 ID/마지막 요청/offset/검증 결과 보관", "verify_final"),
)
FLOW_STAGES: tuple[Stage, ...] = tuple(info.stage for info in FLOW)
POINT_OF_NO_RETURN = Stage.SWITCHING
APPROVAL_STAGE = Stage.READY
WRITE_PAUSE_STAGES = frozenset(
    {Stage.FENCING, Stage.DRAINING, Stage.SYNC_CONFIRMED, Stage.SWITCHING}
)
FAILURE_STATUS = {
    Stage.ABORTED: MigrationStatus.ABORTED,
    Stage.NEEDS_RECOVERY: MigrationStatus.NEEDS_RECOVERY,
    Stage.ROLLED_BACK: MigrationStatus.ROLLED_BACK,
}


def status_of(stage: Stage | None) -> MigrationStatus:
    if stage is None:
        return MigrationStatus.PENDING
    if stage in FAILURE_STATUS:
        return FAILURE_STATUS[stage]
    if stage == Stage.COMPLETED:
        return MigrationStatus.COMPLETED
    if stage == APPROVAL_STAGE:
        return MigrationStatus.WAITING_APPROVAL
    return MigrationStatus.RUNNING


def stage_index(stage: Stage | None, failed_stage: Stage | None = None) -> int:
    effective = failed_stage if stage in FAILURE_STATUS and failed_stage else stage
    if effective in FLOW_STAGES:
        return FLOW_STAGES.index(effective)
    return 0


def progress_of(position: int, current: int, *, failed: bool, finished: bool) -> StageProgress:
    if finished or position < current:
        return StageProgress.DONE
    if position == current:
        return StageProgress.FAILED if failed else StageProgress.CURRENT
    return StageProgress.PENDING
