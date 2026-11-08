// 비용 화면 모델. 개요(AWS Billing 홈·Datadog 비용 요약 방식)와 이전 영향(Infracost 방식) 계산.
import type {
  ChangeKind,
  CostBreakdown,
  CostChange,
  CostLine,
  CostOverview,
  EnvironmentCost,
  MigrationCost,
} from "../../api/costs.ts";
import type { FleetEnvironment } from "../../api/migrations.ts";
import { byCategory } from "./model.ts";

/** 온프렘인데 장비비·전기료가 없으면 같은 사양 AWS 가격(cloud_equivalent)으로 본다. */
export function mainBreakdown(env: EnvironmentCost): {
  breakdown: CostBreakdown | null;
  estimated: boolean;
} {
  const missing =
    env.provider === "onprem" &&
    env.current !== null &&
    env.current.unpriced > 0;
  if (missing && env.cloud_equivalent)
    return { breakdown: env.cloud_equivalent, estimated: true };
  return { breakdown: env.current, estimated: false };
}

export function pickJob(migrations: MigrationCost[], jobId: string) {
  return migrations.some((m) => m.job_id === jobId)
    ? jobId
    : (migrations[0]?.job_id ?? "");
}

export function envTotals(overview: CostOverview) {
  const rows = overview.environments.map((env) => {
    const { breakdown, estimated } = mainBreakdown(env);
    return {
      env,
      monthly: breakdown?.monthly ?? 0,
      estimated,
      unpriced: breakdown?.unpriced ?? 0,
    };
  });
  const total = rows.reduce((n, r) => n + r.monthly, 0);
  const cloud = rows
    .filter((r) => r.env.provider !== "onprem")
    .reduce((n, r) => n + r.monthly, 0);
  return { rows, total, cloud, onprem: total - cloud };
}

export function categoryMatrix(overview: CostOverview) {
  const envs = overview.environments.map((env) => {
    const { breakdown } = mainBreakdown(env);
    return {
      env,
      total: breakdown?.monthly ?? 0,
      parts: breakdown ? byCategory(breakdown.lines) : [],
    };
  });
  const totals = new Map<string, number>();
  for (const e of envs)
    for (const p of e.parts)
      totals.set(p.category, (totals.get(p.category) ?? 0) + p.monthly);
  const categories = [...totals.entries()]
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([c]) => c);
  return { envs, categories, totals };
}

export function topResources(overview: CostOverview, limit = 6) {
  return overview.environments
    .flatMap((env) => {
      const { breakdown } = mainBreakdown(env);
      return (breakdown?.lines ?? []).map((line) => ({ env, line }));
    })
    .filter((r) => (r.line.monthly ?? 0) > 0)
    .sort((a, b) => (b.line.monthly ?? 0) - (a.line.monthly ?? 0))
    .slice(0, limit);
}

export function dataHealth(overview: CostOverview) {
  const unpriced = overview.environments.reduce(
    (n, env) => n + (mainBreakdown(env).breakdown?.unpriced ?? 0),
    0,
  );
  const estimated = overview.environments.filter(
    (env) => mainBreakdown(env).estimated,
  );
  const gaps = overview.environments.flatMap((env) =>
    env.gaps.map((gap) => ({ env, gap })),
  );
  return { unpriced, estimated, gaps };
}

/** Infracost 표: 환경별 지금 → 변화 → 이전 후, 그리고 합계. */
export function compareRows(
  m: MigrationCost,
  estimate?: (envId: string) => number | null,
) {
  const sourceAfter =
    m.source_monthly !== null && m.source_released !== null
      ? m.source_monthly - m.source_released
      : m.source_monthly;
  const targetChange =
    m.target_monthly_after !== null && m.target_monthly_now !== null
      ? m.target_monthly_after - m.target_monthly_now
      : null;
  const rows = [
    {
      role: "원본" as const,
      env: m.source_env,
      now: m.source_monthly,
      change: m.source_released === null ? null : -m.source_released,
      after: sourceAfter,
    },
    {
      role: "대상" as const,
      env: m.target_env,
      now: m.target_monthly_now,
      change: targetChange,
      after: m.target_monthly_after,
    },
  ];
  // 온프렘 장비비가 없어 0으로 온 행: 비용은 그대로이므로 환산값을 지금/이전 후에 같이 쓴다.
  for (const r of rows) {
    const fallback = estimate?.(r.env) ?? null;
    if (fallback !== null && !r.now && !r.after && !r.change) {
      r.now = fallback;
      r.after = fallback;
      r.change = 0;
    }
  }
  const sum = (pick: (r: (typeof rows)[number]) => number | null) =>
    rows.every((r) => pick(r) !== null)
      ? rows.reduce((n, r) => n + (pick(r) ?? 0), 0)
      : null;
  return {
    rows,
    total: {
      now: sum((r) => r.now),
      change: m.monthly_change,
      after: sum((r) => r.after),
    },
  };
}

export const CHANGE_KIND_LABELS: Record<ChangeKind, string> = {
  replaced: "관리형으로 전환",
  moved: "그대로 옮김",
  added: "새로 생김",
  removed: "원본에서 없어짐",
};

/** 자원별 변화: 월 변화가 큰 순(가격 정보 없는 행은 아래). */
export function sortedChanges(changes: CostChange[]) {
  return [...changes].sort(
    (a, b) => (b.delta ?? -Infinity) - (a.delta ?? -Infinity),
  );
}

export function lineLabel(line: CostLine) {
  return line.item;
}

export type EnvLabel = { name: string; provider: string | undefined };

/** 이전 작업의 환경 이름. 비용을 계산한 환경이 먼저고, 직접 연결한 환경은 환경 목록 이름을 쓴다. */
export function envLabels(
  overview: CostOverview,
  fleet: readonly FleetEnvironment[] | undefined,
): (id: string) => EnvLabel {
  const labels = new Map<string, EnvLabel>();
  for (const env of fleet ?? [])
    labels.set(env.id, { name: env.name, provider: env.provider });
  for (const env of overview.environments)
    labels.set(env.env_id, { name: env.name, provider: env.provider });
  return (id) => labels.get(id) ?? { name: id, provider: undefined };
}
