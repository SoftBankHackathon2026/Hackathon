import type {
  Category,
  Control,
  ControlStatus,
  Severity,
} from "../../api/security.ts";
import type { CardLine } from "../../components/SignalCard.tsx";
import type { Tone } from "../migrations/stages.ts";

export const CATEGORY_ORDER: Category[] = [
  "network",
  "workload",
  "identity",
  "data",
  "detection",
  "governance",
];

export const CATEGORY_LABELS: Record<Category, string> = {
  network: "네트워크",
  workload: "워크로드",
  identity: "ID/접근",
  data: "데이터",
  detection: "탐지/감사",
  governance: "거버넌스",
};

export const SEVERITY_ORDER: Severity[] = ["critical", "high", "medium", "low"];

export const SEVERITY_LABELS: Record<Severity, string> = {
  critical: "치명",
  high: "높음",
  medium: "중간",
  low: "낮음",
};

export const STATUS_LABELS: Record<ControlStatus, string> = {
  pass: "통과",
  fail: "실패",
  unknown: "미확인",
  not_applicable: "해당 없음",
};

export type PostureFilters = {
  severity: Severity | "all";
  category: Category | "all";
  status: ControlStatus | "all";
  query: string;
};

export const NO_FILTERS: PostureFilters = {
  severity: "all",
  category: "all",
  status: "all",
  query: "",
};

export function statusTone(status: ControlStatus): Tone {
  switch (status) {
    case "pass":
      return "ok";
    case "fail":
      return "bad";
    case "unknown":
      return "warn";
    default:
      return "idle";
  }
}

export function filterControls(controls: Control[], filters: PostureFilters) {
  const query = filters.query.trim().toLowerCase();
  return controls.filter((control) => {
    if (filters.severity !== "all" && control.severity !== filters.severity)
      return false;
    if (filters.category !== "all" && control.category !== filters.category)
      return false;
    if (
      query &&
      !`${control.id} ${control.title}`.toLowerCase().includes(query)
    )
      return false;
    if (
      filters.status !== "all" &&
      !Object.values(control.results).some((r) => r.status === filters.status)
    )
      return false;
    return true;
  });
}

export function groupByCategory(controls: Control[]) {
  return CATEGORY_ORDER.map((category) => ({
    category,
    controls: controls
      .filter((c) => c.category === category)
      .sort(
        (a, b) =>
          SEVERITY_ORDER.indexOf(a.severity) -
          SEVERITY_ORDER.indexOf(b.severity),
      ),
  })).filter((group) => group.controls.length > 0);
}

export function scoreTone(score: number): Tone {
  if (score >= 80) return "ok";
  if (score >= 50) return "warn";
  return "bad";
}

export function totalChecks(env: {
  passed: number;
  failed: number;
  unknown: number;
}) {
  return env.passed + env.failed + env.unknown;
}

export function hasFailure(control: Control) {
  return Object.values(control.results).some((r) => r.status === "fail");
}

export function standardId(ref: string) {
  return ref.split(" ")[0];
}

export type StandardEnvStats = {
  pass: number;
  fail: number;
  unknown: number;
  total: number;
  score: number | null;
  coverage: number | null;
  failedBySeverity: Record<Severity, number>;
};

export function standardCompliance(
  standards: { id: string; name: string }[],
  controls: Control[],
  envIds: string[],
) {
  return standards.map((standard) => {
    const members = controls.filter((c) =>
      c.standard_refs.some((ref) => standardId(ref) === standard.id),
    );
    const envs: Record<string, StandardEnvStats> = {};
    for (const envId of envIds) {
      const results = members.map((c) => ({
        control: c,
        status: c.results[envId]?.status,
      }));
      const evaluated = results.filter((r) => r.status !== "not_applicable");
      const pass = evaluated.filter((r) => r.status === "pass").length;
      const failed = evaluated.filter((r) => r.status === "fail");
      const unknown = evaluated.length - pass - failed.length;
      const failedBySeverity: Record<Severity, number> = {
        critical: 0,
        high: 0,
        medium: 0,
        low: 0,
      };
      for (const r of failed) failedBySeverity[r.control.severity] += 1;
      envs[envId] = {
        pass,
        fail: failed.length,
        unknown,
        total: evaluated.length,
        score:
          pass + failed.length > 0
            ? Math.round((pass / (pass + failed.length)) * 100)
            : null,
        coverage:
          evaluated.length > 0
            ? (pass + failed.length) / evaluated.length
            : null,
        failedBySeverity,
      };
    }
    return { standard, members: members.length, envs };
  });
}

export const MIN_COVERAGE = 0.5;

export function hasEnoughEvidence(known: number, total: number) {
  return total > 0 && known / total >= MIN_COVERAGE;
}

export function envSecurityLine(env: {
  passed: number;
  failed: number;
  unknown: number;
  score: number;
}): CardLine {
  const total = env.passed + env.failed + env.unknown;
  const unknown = env.unknown > 0 ? `, ${env.unknown}개 미확인` : "";
  const score = hasEnoughEvidence(env.passed + env.failed, total)
    ? `, 보안 점수 ${env.score}`
    : ", 미확인 항목이 많아 점수 없음";
  if (env.failed > 0)
    return {
      tone: "bad",
      text: `점검 ${total}개 중 ${env.failed}개 실패${unknown}${score}`,
    };
  return { tone: "ok", text: `점검 ${total}개 중 실패 없음${unknown}${score}` };
}

export function overviewScore(env: {
  passed: number;
  failed: number;
  unknown: number;
  score: number;
}) {
  const total = env.passed + env.failed + env.unknown;
  return {
    total,
    score: hasEnoughEvidence(env.passed + env.failed, total) ? env.score : null,
    passShare: total > 0 ? Math.round((env.passed / total) * 100) : 0,
  };
}

export function compareSummary(
  parity: {
    regressions: string[];
    improvements: string[];
  },
  controls: Control[],
) {
  const title = (id: string) => controls.find((c) => c.id === id)?.title ?? id;
  const weakened = parity.regressions.map(title);
  return {
    sentence:
      weakened.length > 0
        ? `도착 환경에서 새로 실패한 통제가 ${weakened.length}개 있어요`
        : "도착 환경에서 새로 실패한 통제는 없어요",
    weakened,
    strengthened: parity.improvements.slice(0, 3).map(title),
    moreStrengthened: Math.max(0, parity.improvements.length - 3),
  };
}

export type ScoreBand = "high" | "mid" | "low";

export function scoreBand(score: number): ScoreBand {
  if (score >= 80) return "high";
  if (score >= 50) return "mid";
  return "low";
}

export const BAND_LABELS: Record<ScoreBand, string> = {
  high: "기준 충족",
  mid: "보통",
  low: "기준 미달",
};

export function complianceBar(
  env: { passed: number; failed: number; unknown: number },
  scaleMax: number,
) {
  const total = env.passed + env.failed + env.unknown;
  const make = (key: "pass" | "fail" | "unknown", count: number) => ({
    key,
    count,
    percent: total > 0 ? Math.round((count / total) * 100) : 0,
    width: scaleMax > 0 ? (count / scaleMax) * 100 : 0,
  });
  return {
    total,
    segments: [
      make("pass", env.passed),
      make("fail", env.failed),
      make("unknown", env.unknown),
    ],
  };
}
