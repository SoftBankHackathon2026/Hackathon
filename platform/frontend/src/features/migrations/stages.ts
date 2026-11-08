import type {
  MigrationStatus,
  MigrationSummary,
  StageInfo,
  StageStatus,
} from "../../api/migrations.ts";

export type Tone = "ok" | "running" | "warn" | "bad" | "idle";

export const STAGE_TITLES: Record<string, string> = {
  PRECHECK: "사전 점검",
  COPYING: "초기 복사",
  CATCHING_UP: "변경분 추적",
  READY: "전환 승인 대기",
  FENCING: "원본 쓰기 차단",
  DRAINING: "최종 반영",
  SYNC_CONFIRMED: "동기화 확정",
  SWITCHING: "쓰기 전환",
  VERIFYING: "전환 후 검증",
  COMPLETED: "완료",
  ABORTED: "전환 중단",
  NEEDS_RECOVERY: "복구 필요",
  ROLLED_BACK: "롤백됨",
};

export const PAUSE_STAGES: readonly string[] = [
  "FENCING",
  "DRAINING",
  "SYNC_CONFIRMED",
  "SWITCHING",
];

export const STATUS_LABELS: Record<MigrationStatus, string> = {
  pending: "대기",
  running: "진행 중",
  waiting_approval: "승인 대기",
  completed: "완료",
  aborted: "중단됨",
  needs_recovery: "복구 필요",
  rolled_back: "롤백됨",
};

const ACTIVE_POLL_MS = 5000;
const IDLE_POLL_MS = 30000;

export function stageKey(name: string | null) {
  return name === null ? "" : name.toUpperCase();
}

export function stageTitle(name: string | null) {
  if (name === null) return "시작 전";
  return STAGE_TITLES[stageKey(name)] ?? name;
}

export function isPauseStage(name: string | null) {
  return PAUSE_STAGES.includes(stageKey(name));
}

export function isApprovalStage(name: string) {
  return stageKey(name) === "READY";
}

export function isActive(status: MigrationStatus | undefined) {
  return status === "running" || status === "waiting_approval";
}

export function pollInterval(status: MigrationStatus | undefined) {
  return isActive(status) ? ACTIVE_POLL_MS : IDLE_POLL_MS;
}

export function statusTone(status: MigrationStatus): Tone {
  switch (status) {
    case "completed":
      return "ok";
    case "running":
      return "running";
    case "waiting_approval":
    case "rolled_back":
      return "warn";
    case "aborted":
    case "needs_recovery":
      return "bad";
    default:
      return "idle";
  }
}

export function stageTone(status: StageStatus): Tone {
  switch (status) {
    case "done":
      return "ok";
    case "current":
      return "running";
    case "failed":
      return "bad";
    default:
      return "idle";
  }
}

export function stepLabel(
  summary: Pick<MigrationSummary, "status" | "stage_index" | "stage_count">,
) {
  const done =
    summary.status === "pending"
      ? 0
      : Math.min(summary.stage_index + 1, summary.stage_count);
  return `${done}/${summary.stage_count}`;
}

export function stageSummaryText(
  summary: Pick<
    MigrationSummary,
    "status" | "stage" | "stage_index" | "stage_count"
  >,
) {
  const title =
    summary.status === "waiting_approval"
      ? "전환 승인 대기"
      : stageTitle(summary.stage);
  return `${stepLabel(summary)} ${title}`;
}

export const STAGE_STATUS_LABELS: Record<StageStatus, string> = {
  done: "완료",
  current: "진행 중",
  pending: "대기",
  failed: "실패",
  skipped: "건너뜀",
};

/** 끝난 시각이 없으면 진행 중인 단계만 지금까지 센다. 완료처럼 끝 시각이 없는 마지막 단계는 시간이 흐르지 않는다. */
export function stageDurationSeconds(
  stage: Pick<StageInfo, "started_at" | "finished_at" | "status">,
  now: number,
) {
  if (stage.started_at === null) return null;
  if (stage.finished_at === null && stage.status !== "current") return null;
  const start = Date.parse(stage.started_at);
  const end = stage.finished_at === null ? now : Date.parse(stage.finished_at);
  if (Number.isNaN(start) || Number.isNaN(end)) return null;
  return Math.max(0, (end - start) / 1000);
}

export function stageNotes(
  stage: Pick<StageInfo, "name" | "point_of_no_return">,
) {
  const notes: string[] = [];
  if (isApprovalStage(stage.name)) notes.push("승인 지점");
  if (stage.point_of_no_return) notes.push("되돌릴 수 없는 지점");
  return notes;
}

export function stateTone(state: string): Tone {
  switch (state.toUpperCase()) {
    case "RUNNING":
    case "HEALTHY":
      return "ok";
    case "SWITCHING":
      return "running";
    case "PAUSED":
    case "UNASSIGNED":
      return "warn";
    case "FAILED":
    case "ERROR":
      return "bad";
    default:
      return "idle";
  }
}

const STATE_LABELS: Record<string, string> = {
  RUNNING: "실행 중",
  HEALTHY: "정상",
  PAUSED: "일시 중지",
  SWITCHING: "전환 중",
  UNASSIGNED: "할당 안 됨",
  FAILED: "실패",
  ERROR: "오류",
};

export function stateLabel(state: string) {
  return STATE_LABELS[state.toUpperCase()] ?? state;
}
