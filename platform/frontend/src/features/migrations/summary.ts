import type {
  FleetEnvironment,
  MigrationSummary,
  VerifyReport,
} from "../../api/migrations.ts";
import type { CardLine } from "../../components/SignalCard.tsx";
import type { Posture } from "../../api/security.ts";
import { formatCount, formatSeconds } from "../../lib/format.ts";
import { isActive, type Tone } from "./stages.ts";
import { verifyTotal } from "./proof.ts";

export function jobCounts(migrations: MigrationSummary[]) {
  return {
    total: migrations.length,
    active: migrations.filter((m) => isActive(m.status)).length,
    waiting: migrations.filter((m) => m.status === "waiting_approval").length,
  };
}

export function failedWrites(m: Pick<MigrationSummary, "writes" | "downtime">) {
  return m.writes.failed + m.downtime.failed_requests;
}

export function writeTotals(migrations: MigrationSummary[]) {
  return migrations.reduce(
    (acc, m) => ({
      failed: acc.failed + failedWrites(m),
      rejected: acc.rejected + m.writes.rejected,
    }),
    { failed: 0, rejected: 0 },
  );
}

export function longestPause(migrations: MigrationSummary[]) {
  let best: { seconds: number; jobId: string } | null = null;
  for (const m of migrations) {
    const seconds = m.downtime.write_pause_seconds;
    if (seconds !== null && (best === null || seconds > best.seconds))
      best = { seconds, jobId: m.job_id };
  }
  return best;
}

export function fleetVerify(migrations: MigrationSummary[]) {
  const checked = migrations.filter((m) => m.verify_mismatch !== null);
  const mismatch = migrations.reduce(
    (sum, m) => sum + m.writes.mismatch + (m.verify_mismatch ?? 0),
    0,
  );
  return { checked: checked.length, total: migrations.length, mismatch };
}

export function jobVerify(pre: VerifyReport | null, post: VerifyReport | null) {
  const report = post ?? pre;
  if (!report) return { state: "none" as const, mismatch: 0, phase: "" };
  const mismatch = verifyTotal(report) ?? 0;
  return {
    state:
      report.passed && mismatch === 0 ? ("pass" as const) : ("fail" as const),
    mismatch,
    phase: post ? "전환 후" : "전환 전",
  };
}

export function postureCompare(posture: Posture | undefined) {
  if (!posture) return null;
  const { parity, environments } = posture;
  const score = (id: string) =>
    environments.find((e) => e.env_id === id)?.score ?? null;
  return {
    regressions: parity.regressions.length,
    improvements: parity.improvements.length,
    from: score(parity.source_env),
    to: score(parity.target_env),
  };
}

export function activeJobsLine(counts: ReturnType<typeof jobCounts>): CardLine {
  if (counts.active === 0)
    return {
      tone: "none",
      text:
        counts.total === 0
          ? "진행 중인 작업이 없어요"
          : `진행 중인 작업이 없어요. 전체는 ${counts.total}건이에요`,
    };
  if (counts.waiting > 0)
    return {
      tone: "warn",
      text: `${counts.active}건 진행 중, ${counts.waiting}건은 승인을 기다립니다`,
    };
  return { tone: "running", text: `${counts.active}건 진행 중` };
}

export function writeFailureLine(
  totals: ReturnType<typeof writeTotals>,
): CardLine {
  if (totals.failed > 0)
    return {
      tone: "bad",
      text:
        totals.rejected > 0
          ? `${formatCount(totals.failed)}건 실패, ${formatCount(totals.rejected)}건 거절`
          : `${formatCount(totals.failed)}건 실패`,
    };
  return { tone: "ok", text: "실패한 쓰기가 없어요" };
}

export function pauseLine(pause: ReturnType<typeof longestPause>): CardLine {
  if (!pause) return { tone: "none", text: "아직 전환한 작업이 없어요" };
  return {
    tone: "none",
    text: `${formatSeconds(pause.seconds)} (${pause.jobId})`,
  };
}

export function fleetVerifyLine(
  verify: ReturnType<typeof fleetVerify>,
): CardLine {
  if (verify.checked === 0)
    return { tone: "none", text: "아직 검증한 작업이 없어요" };
  if (verify.mismatch > 0)
    return {
      tone: "bad",
      text: `데이터 ${formatCount(verify.mismatch)}건 불일치`,
    };
  return {
    tone: "ok",
    text:
      verify.checked === verify.total
        ? `검증한 ${verify.checked}건 모두 일치해요`
        : `${verify.total}건 중 ${verify.checked}건을 검증했고 모두 일치해요`,
  };
}

export function securityCompareLine(
  compare: ReturnType<typeof postureCompare>,
): CardLine {
  if (!compare) return { tone: "none", text: "-" };
  const score =
    compare.from !== null && compare.to !== null
      ? `, 보안 점수 ${compare.from}에서 ${compare.to}로`
      : "";
  if (compare.regressions > 0)
    return {
      tone: "bad",
      text: `도착 환경에서 새로 실패한 통제 ${compare.regressions}개${score}`,
    };
  return { tone: "ok", text: `새로 실패한 통제 없음${score}` };
}

export function envOverviewLine(environments: FleetEnvironment[]) {
  const withIssues = environments.filter((e) => e.issues.length > 0).length;
  return withIssues > 0
    ? `환경 ${environments.length}곳 중 ${withIssues}곳에 경고가 있어요.`
    : `환경 ${environments.length}곳 모두 경고가 없어요.`;
}

export type OverviewCard = {
  title: string;
  label: { tone: "ok" | "bad"; text: string } | null;
  value: string;
  unit: string;
  lines: string[];
};

export function splitUnit(text: string) {
  const match = /^([\d.,]+)(.*)$/.exec(text);
  return match
    ? { value: match[1], unit: match[2] }
    : { value: text, unit: "" };
}

export function filterJobs(
  migrations: MigrationSummary[],
  envId: string,
  status: "all" | "active" | "waiting" | "done",
) {
  return migrations.filter((m) => {
    if (envId !== "all" && m.source_env !== envId && m.target_env !== envId)
      return false;
    if (status === "active") return isActive(m.status);
    if (status === "waiting") return m.status === "waiting_approval";
    if (status === "done") return m.status === "completed";
    return true;
  });
}

export function overviewCards(migrations: MigrationSummary[]): OverviewCard[] {
  const failed = writeTotals(migrations).failed;
  const pause = longestPause(migrations);
  const verify = fleetVerify(migrations);
  const pauseText = splitUnit(pause ? formatSeconds(pause.seconds) : "-");
  return [
    {
      title: "실패한 쓰기",
      label:
        migrations.length === 0
          ? null
          : failed > 0
            ? { tone: "bad", text: "실패 있음" }
            : { tone: "ok", text: "무중단" },
      value: formatCount(failed),
      unit: "건",
      lines:
        migrations.length === 0
          ? ["해당하는 작업이 없어요"]
          : migrations
              .slice(0, 3)
              .map((m) => `${m.job_id} ${formatCount(failedWrites(m))}건`),
    },
    {
      title: "최대 쓰기 중단",
      label: null,
      value: pauseText.value,
      unit: pauseText.unit,
      lines: pause
        ? [`${pause.jobId}, FENCING–SWITCHING`]
        : ["아직 전환한 작업이 없어요"],
    },
    {
      title: "데이터 일치",
      label: verify.mismatch > 0 ? { tone: "bad", text: "실패 있음" } : null,
      value: formatCount(verify.mismatch),
      unit: "건 불일치",
      lines: [
        verify.total === 0
          ? "해당하는 작업이 없어요"
          : `검증 ${verify.total}건 중 ${verify.checked}건 완료`,
      ],
    },
  ];
}

export type SummaryItem = {
  label: string;
  value: string;
  unit?: string;
  tone?: Tone;
  note?: string;
};

/** 이전 현황 한 줄 요약과 전체 상태. 문제가 있는 항목만 색을 준다. */
export function overviewSummary(migrations: MigrationSummary[]): {
  status: { tone: Tone; text: string } | null;
  items: SummaryItem[];
} {
  const failed = writeTotals(migrations).failed;
  const pause = longestPause(migrations);
  const verify = fleetVerify(migrations);
  const pauseText = splitUnit(pause ? formatSeconds(pause.seconds) : "-");
  const problems = (failed > 0 ? 1 : 0) + (verify.mismatch > 0 ? 1 : 0);
  return {
    status:
      migrations.length === 0
        ? null
        : problems > 0
          ? { tone: "bad", text: "실패 있음" }
          : { tone: "ok", text: "실패 없음" },
    items: [
      {
        label: "실패한 쓰기",
        value: formatCount(failed),
        unit: "건",
        tone: failed > 0 ? "bad" : undefined,
      },
      {
        label: "최대 쓰기 중단",
        value: pauseText.value,
        unit: pauseText.unit,
        note: pause ? pause.jobId : "전환 전",
      },
      {
        label: "데이터 불일치",
        value: formatCount(verify.mismatch),
        unit: "건",
        tone: verify.mismatch > 0 ? "bad" : undefined,
        note:
          verify.total === 0
            ? undefined
            : `검증 ${verify.total}건 중 ${verify.checked}건 완료`,
      },
    ],
  };
}
