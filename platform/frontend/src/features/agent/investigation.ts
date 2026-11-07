import type {
  AgentRun,
  AgentRunDetail,
  AgentRunEvent,
} from "../../api/agent.ts";
import type { Tone } from "../migrations/stages.ts";
import { type ChangeRow, changeRows, gateView } from "./model.ts";

export type StepKey =
  "detect" | "diagnose" | "propose" | "approve" | "probe" | "apply" | "verify";

export type StepNote = { label: string; text: string };

export type Step = {
  key: StepKey;
  title: string;
  tone: Tone;
  reached: boolean;
  text: string;
  codes: string[];
  changes: ChangeRow[];
  notes: StepNote[];
  at: string | null;
};

const TITLES: Record<StepKey, string> = {
  detect: "감지",
  diagnose: "진단",
  propose: "원인과 수정 제안",
  approve: "승인",
  probe: "적용 전 확인",
  apply: "적용",
  verify: "재검증",
};

const PROBE_STEPS: Record<string, string> = {
  dns: "주소 찾기",
  connect: "연결",
  login: "로그인",
  tls: "TLS",
  query: "조회",
};

const SKIPPED = "진행하지 않았어요.";

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown) {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

function strings(value: unknown) {
  return Array.isArray(value) ? value.map(String) : [];
}

function step(key: StepKey, fields: Partial<Step> = {}): Step {
  return {
    key,
    title: TITLES[key],
    tone: "idle",
    reached: false,
    text: SKIPPED,
    codes: [],
    changes: [],
    notes: [],
    at: null,
    ...fields,
  };
}

function seconds(from: string, to: string) {
  const ms = Date.parse(to) - Date.parse(from);
  return Number.isFinite(ms) && ms >= 0 ? Math.round(ms / 1000) : null;
}

function find(events: AgentRunEvent[], match: (e: AgentRunEvent) => boolean) {
  return events.find(match) ?? null;
}

function probeStep(value: unknown) {
  const name = String(value ?? "");
  return PROBE_STEPS[name] ?? name;
}

function detect(run: AgentRunDetail): Step {
  const gate = gateView(run.detail.pre_decision);
  if (gate === null)
    return step("detect", {
      reached: true,
      text: "판정 기록이 없어요.",
      at: run.created_at,
    });
  if (gate.outcome === "allow")
    return step("detect", {
      reached: true,
      tone: "ok",
      text: "대상 앱 검증이 통과했어요 (OPA allow).",
      at: run.created_at,
    });
  return step("detect", {
    reached: true,
    tone: "bad",
    text:
      gate.outcome === "hold"
        ? "대상 앱 검증을 끝내지 못해 판정을 보류했어요 (OPA hold)."
        : `대상 앱 검증에서 문제를 찾았어요 (OPA ${gate.outcome}).`,
    codes: gate.reasonCodes,
    at: run.created_at,
  });
}

function diagnose(run: AgentRunDetail): Step {
  const diagnosis = record(run.detail.diagnosis);
  if (typeof diagnosis.ok !== "boolean") {
    const proposed = changeRows(run.detail).length > 0;
    return step("diagnose", {
      text: proposed
        ? "이 실행은 진단 기록을 남기지 않았어요."
        : "AI가 고칠 수 있는 범위가 아니어서 진단하지 않았어요.",
    });
  }
  if (diagnosis.ok)
    return step("diagnose", {
      reached: true,
      tone: "warn",
      text: "지금 설정으로는 DB에 접속돼요. 설정 문제가 아닐 수 있어요.",
    });
  const detail = text(diagnosis.detail) ?? "원인을 특정하지 못했어요";
  return step("diagnose", {
    reached: true,
    tone: "ok",
    text: `지금 설정으로 접속하면 ${probeStep(diagnosis.step)} 단계에서 실패해요: ${detail}`,
  });
}

function propose(run: AgentRunDetail, approved: boolean): Step {
  const proposal = record(run.detail.proposal);
  const changes = changeRows(run.detail);
  const rejections = strings(run.detail.rejections);
  const notes: StepNote[] = [];
  const impact = text(proposal.impact);
  const checked = text(proposal.verification);
  if (impact) notes.push({ label: "영향", text: impact });
  if (checked) notes.push({ label: "샌드박스 확인", text: checked });
  if (rejections.length > 0 && !approved) {
    const last = run.events.at(-1);
    return step("propose", {
      reached: true,
      tone: "bad",
      text: last?.message ?? "정적 검사에서 제안을 거절했어요.",
      codes: rejections,
      changes,
      notes,
    });
  }
  if (changes.length === 0) return step("propose");
  return step("propose", {
    reached: true,
    tone: "ok",
    text: text(proposal.cause) ?? "원인 설명이 없어요.",
    changes,
    notes,
    at: find(run.events, (e) => e.state === "WAITING_APPROVAL")?.at ?? null,
  });
}

function approve(run: AgentRunDetail): Step {
  const waiting = find(run.events, (e) => e.state === "WAITING_APPROVAL");
  const applying = find(run.events, (e) => e.state === "APPLYING");
  const refused = find(
    run.events,
    (e) => e.state === "REJECTED" && e.message.includes("사람이 거절"),
  );
  if (applying && applying.message.startsWith("자동 적용"))
    return step("approve", {
      reached: true,
      tone: "ok",
      text: "자동 적용 모드라 바로 진행했어요.",
      at: applying.at,
    });
  if (applying) {
    const waited = waiting ? seconds(waiting.at, applying.at) : null;
    return step("approve", {
      reached: true,
      tone: "ok",
      text: applying.message,
      notes:
        waited === null ? [] : [{ label: "기다린 시간", text: `${waited}초` }],
      at: applying.at,
    });
  }
  if (refused)
    return step("approve", {
      reached: true,
      tone: "warn",
      text: refused.message,
      at: refused.at,
    });
  if (run.state === "WAITING_APPROVAL")
    return step("approve", {
      reached: true,
      tone: "running",
      text: "사람 승인을 기다리고 있어요.",
      at: waiting?.at ?? null,
    });
  return step("approve");
}

function probe(run: AgentRunDetail): Step {
  const result = record(run.detail.probe);
  const recheck = find(run.events, (e) =>
    e.message.startsWith("승인 시 재검사"),
  );
  if (recheck)
    return step("probe", {
      reached: true,
      tone: "bad",
      text: recheck.message,
      codes: strings(run.detail.rejections),
      at: recheck.at,
    });
  if (typeof result.ok !== "boolean") return step("probe");
  if (result.ok)
    return step("probe", {
      reached: true,
      tone: "ok",
      text: "제안 값으로 대상 DB에 접속하고 조회까지 했어요.",
    });
  return step("probe", {
    reached: true,
    tone: "bad",
    text: `제안 값으로도 ${probeStep(result.step)} 단계에서 실패했어요 (${String(result.error ?? "원인 모름")}). 저장소는 그대로예요.`,
  });
}

function apply(run: AgentRunDetail): Step {
  const written = find(
    run.events,
    (e) => e.state === "APPLYING" && e.message.startsWith("저장소"),
  );
  const last = run.events.at(-1);
  if (run.state === "CAS_CONFLICT")
    return step("apply", {
      reached: true,
      tone: "bad",
      text: last?.message ?? "저장소 값이 바뀌어 쓰지 않았어요.",
    });
  if (run.detail.applied_version === undefined) return step("apply");
  const from = run.detail.expected_version;
  const to = run.detail.applied_version;
  if (run.state === "RESTART_FAILED")
    return step("apply", {
      reached: true,
      tone: "bad",
      text: last?.message ?? "앱을 다시 시작하지 못했어요.",
      at: written?.at ?? null,
    });
  return step("apply", {
    reached: true,
    tone: "ok",
    text: `설정 저장소를 v${String(from)} → v${String(to)} 로 바꾸고 앱을 다시 시작했어요.`,
    at: written?.at ?? null,
  });
}

function verify(run: AgentRunDetail): Step {
  const gate = gateView(run.detail.post_decision);
  const at = run.state === "RESOLVED" || run.state === "UNRESOLVED";
  if (gate === null)
    return run.state === "APPLYING"
      ? step("verify", {
          reached: true,
          tone: "running",
          text: "적용 결과를 다시 판정하고 있어요.",
        })
      : step("verify");
  if (gate.outcome === "allow")
    return step("verify", {
      reached: true,
      tone: "ok",
      text: "다시 판정했더니 대상 앱 검증이 통과했어요 (OPA allow).",
      at: at ? run.updated_at : null,
    });
  return step("verify", {
    reached: true,
    tone: "bad",
    text: `다시 판정해도 통과하지 못했어요 (OPA ${gate.outcome}).`,
    codes: gate.reasonCodes,
    at: at ? run.updated_at : null,
  });
}

export function investigation(run: AgentRunDetail): Step[] {
  const approval = approve(run);
  const approved = approval.tone === "ok";
  return [
    detect(run),
    diagnose(run),
    propose(run, approved),
    approval,
    probe(run),
    apply(run),
    verify(run),
  ];
}

export function foldSteps(steps: Step[], finished: boolean) {
  if (!finished) return { shown: steps, folded: 0 };
  let end = 0;
  steps.forEach((item, index) => {
    if (item.reached) end = index + 1;
  });
  return { shown: steps.slice(0, end), folded: steps.length - end };
}

const FINISHED = new Set([
  "NOTHING_TO_FIX",
  "RESOLVED",
  "UNRESOLVED",
  "HANDED_OFF",
  "REJECTED",
  "PROBE_FAILED",
  "CAS_CONFLICT",
  "RESTART_FAILED",
  "ERROR",
]);

export function runSeconds(run: AgentRun) {
  return FINISHED.has(run.state)
    ? seconds(run.created_at, run.updated_at)
    : null;
}

export type Verdict = { tone: Tone; title: string; text: string };

export function verdict(run: AgentRunDetail): Verdict {
  const last = run.events.at(-1)?.message ?? "";
  const changes = changeRows(run.detail);
  const changed = changes
    .map((change) => `${change.key} 값을 ${change.after} 값으로`)
    .join(", ");
  switch (run.state) {
    case "RESOLVED":
      return {
        tone: "ok",
        title: "AI가 고쳤어요",
        text: changed
          ? `${changed} 바꿔서 대상 앱 검증을 다시 통과시켰어요.`
          : last,
      };
    case "NOTHING_TO_FIX":
      return { tone: "idle", title: "고칠 것이 없었어요", text: last };
    case "WAITING_APPROVAL":
      return {
        tone: "warn",
        title: "승인을 기다려요",
        text: changed ? `${changed} 바꾸자고 제안했어요.` : last,
      };
    case "CHECKING":
    case "APPLYING":
      return { tone: "running", title: "진행 중이에요", text: last };
    case "HANDED_OFF":
      return { tone: "warn", title: "사람에게 넘겼어요", text: last };
    case "REJECTED":
      return { tone: "warn", title: "적용하지 않았어요", text: last };
    default:
      return { tone: "bad", title: "고치지 못했어요", text: last };
  }
}

export type AgentScore = {
  detected: number;
  proposed: number;
  resolved: number;
  handedOff: number;
  waiting: number;
  medianSeconds: number | null;
};

export function agentScore(runs: AgentRun[]): AgentScore {
  const acted = runs.filter((run) => run.state !== "NOTHING_TO_FIX");
  const resolved = acted.filter((run) => run.state === "RESOLVED");
  const durations = resolved
    .map(runSeconds)
    .filter((value): value is number => value !== null)
    .sort((a, b) => a - b);
  const mid = Math.floor(durations.length / 2);
  const median =
    durations.length === 0
      ? null
      : durations.length % 2 === 1
        ? durations[mid]
        : Math.round((durations[mid - 1] + durations[mid]) / 2);
  return {
    detected: acted.length,
    proposed: acted.filter(
      (run) => run.state === "RESOLVED" || run.summary.changes.length > 0,
    ).length,
    resolved: resolved.length,
    handedOff: acted.filter((run) => run.state === "HANDED_OFF").length,
    waiting: acted.filter((run) => run.state === "WAITING_APPROVAL").length,
    medianSeconds: median,
  };
}
