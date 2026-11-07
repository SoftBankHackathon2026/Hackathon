import type { PlanStep, StepStatus } from "../../api/taskPlan";
import type { Tone } from "./stages";

export const STEP_STATUS_LABELS: Record<StepStatus, string> = {
  pending: "대기",
  running: "진행 중",
  waiting: "승인 대기",
  done: "완료",
  failed: "실패",
  unsupported: "지원 안 함",
};

export const STEP_STATUS_TONES: Record<StepStatus, Tone> = {
  pending: "idle",
  running: "running",
  waiting: "warn",
  done: "ok",
  failed: "bad",
  unsupported: "idle",
};

export function stepNote(step: PlanStep): string | null {
  if (step.status === "unsupported") return "이 방향은 아직 지원하지 않아요.";
  if (!step.runnable && step.status === "pending")
    return "플랫폼 실행 연결 전이에요. 도구를 직접 실행해요.";
  return null;
}
