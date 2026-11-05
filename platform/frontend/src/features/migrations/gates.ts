import type { Gate, GateOutcome, GateStage } from "../../api/migrations.ts";
import type { Tone } from "./stages.ts";

export const GATE_STAGES: { id: GateStage; title: string }[] = [
  { id: "plan", title: "계획" },
  { id: "pre_start", title: "시작 전" },
  { id: "pre_cutover", title: "전환 전" },
  { id: "final_verify", title: "최종 검증" },
];

export const OUTCOME_LABELS: Record<GateOutcome, string> = {
  allow: "허용",
  deny: "거절",
  hold: "보류",
  invalid: "형식 오류",
};

export function outcomeTone(outcome: GateOutcome): Tone {
  switch (outcome) {
    case "allow":
      return "ok";
    case "deny":
      return "bad";
    case "hold":
      return "warn";
    default:
      return "idle";
  }
}

const RULE_LABELS: [string, string][] = [
  ["SEC-01", "대상 DB가 외부에 노출됨"],
  ["SEC-02", "승인되지 않은 위치"],
  ["APP-01", "대상 앱의 DB 연결 또는 주문 조회 실패"],
  ["OPS-01", "최종 동기화가 끝나지 않음"],
  ["OPS-02", "쓰기 대기 시간이 상한을 넘음"],
  ["EVIDENCE-MISSING", "필수 근거 없음"],
  ["EVIDENCE-INVALID", "근거 형식 오류"],
  ["EVIDENCE-STALE", "근거가 오래됨"],
  ["EVIDENCE-PLAN-MISMATCH", "근거가 현재 계획과 다름"],
];

export function reasonLabel(code: string) {
  const upper = code.toUpperCase();
  const exact = RULE_LABELS.find(([prefix]) => upper === prefix);
  if (exact) return exact[1];
  const byPrefix = RULE_LABELS.find(([prefix]) => upper.startsWith(prefix));
  if (byPrefix) {
    if (upper.endsWith("UNVERIFIED")) return `${byPrefix[1]} (미확인)`;
    return byPrefix[1];
  }
  if (upper.endsWith("UNVERIFIED")) return "근거를 확인하지 못함";
  return code;
}

export function latestGates(gates: Gate[]) {
  const latest = new Map<GateStage, Gate>();
  for (const gate of gates) {
    const current = latest.get(gate.stage);
    if (
      !current ||
      Date.parse(gate.evaluated_at) >= Date.parse(current.evaluated_at)
    )
      latest.set(gate.stage, gate);
  }
  return latest;
}

export function eventTitle(title: string) {
  const match = /^정책 게이트 (\w+): (\w+)$/.exec(title);
  if (!match) return title;
  const stage = GATE_STAGES.find((s) => s.id === match[1])?.title ?? match[1];
  const outcome = OUTCOME_LABELS[match[2] as GateOutcome] ?? match[2];
  return `정책 게이트 · ${stage} 단계 ${outcome}`;
}
