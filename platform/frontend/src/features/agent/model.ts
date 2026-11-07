import type { AgentChange, AgentRun, AgentRunState } from "../../api/agent.ts";
import type { Tone } from "../migrations/stages.ts";

export const RUN_STATE_LABELS: Record<AgentRunState, string> = {
  NOTHING_TO_FIX: "문제 없음",
  CHECKING: "확인 중",
  WAITING_APPROVAL: "승인 대기",
  APPLYING: "적용 중",
  RESOLVED: "복구됨",
  UNRESOLVED: "복구 안 됨",
  HANDED_OFF: "사람에게 넘김",
  REJECTED: "거절됨",
  PROBE_FAILED: "탐침 실패",
  CAS_CONFLICT: "값 충돌",
  RESTART_FAILED: "재시작 실패",
  ERROR: "오류",
};

const RUN_TONES: Record<AgentRunState, Tone> = {
  NOTHING_TO_FIX: "idle",
  CHECKING: "running",
  WAITING_APPROVAL: "warn",
  APPLYING: "running",
  RESOLVED: "ok",
  UNRESOLVED: "bad",
  HANDED_OFF: "warn",
  REJECTED: "warn",
  PROBE_FAILED: "bad",
  CAS_CONFLICT: "bad",
  RESTART_FAILED: "bad",
  ERROR: "bad",
};

export function runStateLabel(state: string) {
  return RUN_STATE_LABELS[state as AgentRunState] ?? state;
}

export function runTone(state: string): Tone {
  return RUN_TONES[state as AgentRunState] ?? "idle";
}

export function approvalModeLabel(autoApply: boolean) {
  return autoApply ? "자동 적용" : "사람 승인";
}

export const DECISION_LABELS = { approve: "승인", reject: "거절" } as const;

export type RunFilter = "acted" | "all";

export function filterRuns(runs: AgentRun[], filter: RunFilter) {
  return filter === "all"
    ? runs
    : runs.filter((run) => run.state !== "NOTHING_TO_FIX");
}

export function reasonText(codes: string[]) {
  return codes.length > 0 ? codes.join(", ") : "-";
}

export function changeText(changes: AgentChange[], shown = 2) {
  if (changes.length === 0) return "-";
  const head = changes
    .slice(0, shown)
    .map((change) => `${change.key} → ${change.value}`)
    .join(", ");
  const rest = changes.length - shown;
  return rest > 0 ? `${head} 외 ${rest}개` : head;
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown) {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

export type ChangeRow = { key: string; before: string | null; after: string };

export function changeRows(detail: Record<string, unknown>): ChangeRow[] {
  const changes = record(detail.proposal).changes;
  if (!Array.isArray(changes)) return [];
  return changes.flatMap((raw) => {
    const change = record(raw);
    if (typeof change.key !== "string" || change.value === undefined) return [];
    const before = change.before ?? change.previous;
    return [
      {
        key: change.key,
        before: before === undefined || before === null ? null : String(before),
        after: String(change.value),
      },
    ];
  });
}

export type GateView = { outcome: string; reasonCodes: string[] };

export function gateView(value: unknown): GateView | null {
  const decision = record(value);
  const outcome = text(decision.outcome);
  if (outcome === null) return null;
  const codes = Array.isArray(decision.reason_codes)
    ? decision.reason_codes.map(String)
    : [];
  return { outcome, reasonCodes: codes };
}
