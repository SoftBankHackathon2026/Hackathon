// 구성 요소 상세 패널 모델(Cloudcraft/Datadog 리소스 패널 방식): 상태, 연결, 이전 작업, 보안, 비용을 한곳에 모은다.
import type { EnvironmentCost } from "../../api/costs.ts";
import type {
  EnvComponent,
  FleetEnvironment,
  MigrationSummary,
} from "../../api/migrations.ts";
import { migrationPath } from "./canvasModel.ts";
import { levelOf } from "./blockStyle.ts";

/** "워크로드 6개, 준비 5/5, 메모" → 준비 수, 워크로드 수, 나머지 메모. */
export function readiness(detail: string) {
  const parts = detail
    .split(", ")
    .map((p) => p.trim())
    .filter(Boolean);
  let ready: { ready: number; total: number } | null = null;
  let workloads: number | null = null;
  const notes: string[] = [];
  for (const part of parts) {
    const r = /^준비 (\d+)\/(\d+)$/.exec(part);
    const w = /^워크로드 (\d+)개$/.exec(part);
    if (r) ready = { ready: Number(r[1]), total: Number(r[2]) };
    else if (w) workloads = Number(w[1]);
    else if (part === "관리형 서비스 상태는 아직 수집하지 않음")
      notes.push("관리형 서비스라 상태를 수집하지 않아요");
    else notes.push(part);
  }
  return { ready, workloads, notes };
}

/** 배치 경로: 환경 경계(계정, 리전) + 구성 요소 배치(VPC, 서브넷, 클러스터, 네임스페이스). 단계별 색은 경계 종류로. */
export function placementTrail(env: FleetEnvironment, component: EnvComponent) {
  const labels = [...(env.boundary ?? []), ...(component.placement ?? [])];
  return labels.map((label) => ({ label, level: levelOf(label) }));
}

/** 관리형 서비스의 월 비용: 비용 항목 이름 끝(" / 자원 이름")이 구성 요소 이름의 자원 이름과 같은 것. */
export function componentCost(
  component: EnvComponent,
  envCost: EnvironmentCost | undefined,
) {
  if (!envCost?.current) return null;
  const resource = component.name.split(" ").pop() ?? "";
  if (!component.managed || resource.length < 3) return null;
  const lines = envCost.current.lines.filter(
    (line) => line.item.split(" / ").pop() === resource,
  );
  if (lines.length === 0) return null;
  const monthly = lines.reduce((n, l) => n + (l.monthly ?? 0), 0);
  return { monthly, lines };
}

export type MigrationRole = {
  job: MigrationSummary;
  role: "원본 DB" | "대상 DB" | "이전 도구" | "환경 간 연결";
};

/** 이 구성 요소가 어떤 이전 작업의 데이터 경로에 있나. */
export function migrationRoles(
  env: FleetEnvironment,
  component: EnvComponent,
  migrations: MigrationSummary[],
  environments: FleetEnvironment[],
): MigrationRole[] {
  const same = (n: { envId: string; id: string } | null) =>
    n !== null && n.envId === env.id && n.id === component.id;
  return migrations.flatMap((job): MigrationRole[] => {
    const path = migrationPath(job, environments, null);
    if (same(path.source)) return [{ job, role: "원본 DB" as const }];
    if (same(path.target)) return [{ job, role: "대상 DB" as const }];
    if (same(path.worker) || path.cdc.some(same))
      return [
        {
          job,
          role:
            component.layer === "link"
              ? ("환경 간 연결" as const)
              : ("이전 도구" as const),
        },
      ];
    return [];
  });
}
