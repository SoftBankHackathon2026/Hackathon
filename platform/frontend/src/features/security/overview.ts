// 보안 개요 탭(Wiz 대시보드 방식) 위젯 모델. 범위는 고른 환경들.
import type { FleetEnvironment } from "../../api/migrations.ts";
import type {
  Category,
  Control,
  Posture,
  Severity,
} from "../../api/security.ts";
import { ALL_CATEGORIES, ALL_SEVERITIES } from "./explorer.ts";
import { impactPath, type PathNode } from "./pathModel.ts";

const SEV_RANK: Record<Severity, number> = {
  critical: 4,
  high: 3,
  medium: 2,
  low: 1,
};

function failingIn(control: Control, envIds: string[]) {
  return envIds.filter((id) => control.results[id]?.status === "fail");
}

export function severityTotals(controls: Control[], envIds: string[]) {
  const failing = controls.filter((c) => failingIn(c, envIds).length > 0);
  return ALL_SEVERITIES.map((severity) => ({
    severity,
    count: failing.filter((c) => c.severity === severity).length,
  }));
}

export function categoryTotals(controls: Control[], envIds: string[]) {
  const failing = controls.filter((c) => failingIn(c, envIds).length > 0);
  return ALL_CATEGORIES.map((category: Category) => ({
    category,
    count: failing.filter((c) => c.category === category).length,
    total: controls.filter((c) => c.category === category).length,
  }));
}

/** 심각도 → 실패 환경 수 → 영향 리소스 수 순으로 가장 시급한 실패 통제. */
export function topFailing(controls: Control[], envIds: string[], limit = 5) {
  return controls
    .map((control) => {
      const envs = failingIn(control, envIds);
      const resources = new Set(
        envs.flatMap((id) => control.results[id]?.resources ?? []),
      ).size;
      return { control, envs, resources };
    })
    .filter((row) => row.envs.length > 0)
    .sort(
      (a, b) =>
        SEV_RANK[b.control.severity] - SEV_RANK[a.control.severity] ||
        b.envs.length - a.envs.length ||
        b.resources - a.resources,
    )
    .slice(0, limit);
}

export type ExposedEntry = {
  envId: string;
  entry: PathNode;
  controls: Control[];
  workloads: number;
  data: number;
  worst: Severity;
};

/**
 * 인터넷에 노출된 진입점: 실패 통제의 영향 경로에 들어간 진입점마다
 * 뒤에 붙은 워크로드·데이터 수와 걸린 통제를 모은다.
 */
export function exposedEntries(
  posture: Posture,
  fleetEnvs: FleetEnvironment[],
  envIds: string[],
): ExposedEntry[] {
  const acc = new Map<
    string,
    ExposedEntry & { w: Set<string>; d: Set<string> }
  >();
  for (const control of posture.controls)
    for (const envId of failingIn(control, envIds)) {
      const env = fleetEnvs.find((e) => e.id === envId);
      if (!env) continue;
      const { columns } = impactPath(
        env.components,
        control.results[envId]?.resources ?? [],
      );
      const entries = columns.find((c) => c.key === "entry")?.nodes ?? [];
      const workloads = columns.find((c) => c.key === "workload")?.nodes ?? [];
      const data = columns.find((c) => c.key === "data")?.nodes ?? [];
      for (const entry of entries) {
        const key = `${envId}:${entry.id}`;
        const row = acc.get(key) ?? {
          envId,
          entry,
          controls: [],
          workloads: 0,
          data: 0,
          worst: control.severity,
          w: new Set(),
          d: new Set(),
        };
        if (!row.controls.includes(control)) row.controls.push(control);
        workloads.forEach((n) => row.w.add(n.id));
        data.forEach((n) => row.d.add(n.id));
        if (SEV_RANK[control.severity] > SEV_RANK[row.worst])
          row.worst = control.severity;
        row.entry = { ...row.entry, hit: row.entry.hit || entry.hit };
        acc.set(key, row);
      }
    }
  return [...acc.values()]
    .map(({ w, d, ...row }) => ({ ...row, workloads: w.size, data: d.size }))
    .sort(
      (a, b) =>
        SEV_RANK[b.worst] - SEV_RANK[a.worst] ||
        b.controls.length - a.controls.length ||
        b.workloads - a.workloads,
    );
}
