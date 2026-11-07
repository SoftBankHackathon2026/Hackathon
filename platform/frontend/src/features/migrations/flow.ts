// 마이그레이션 개요 모델: 이전 흐름(단계 묶음별 작업 위치)과 먼저 볼 것(주의 타일).
// Datadog Fleet Automation·Data Jobs 개요처럼 "지금 어디까지 왔나"와 "무엇을 봐야 하나"를 한 화면에 둔다.
import type {
  FleetEnvironment,
  MigrationSummary,
} from "../../api/migrations.ts";
import { appCell } from "./argo.ts";
import { stageKey, type Tone } from "./stages.ts";

export type PhaseId =
  "prepare" | "precheck" | "copy" | "approve" | "switch" | "verify";

export type Phase = {
  id: PhaseId;
  title: string;
  description: string;
  stages: string[];
  pause?: boolean;
  approval?: boolean;
};

export const PHASES: Phase[] = [
  {
    id: "prepare",
    title: "준비",
    description: "환경을 연결하고 옮길 앱을 고릅니다.",
    stages: [""],
  },
  {
    id: "precheck",
    title: "사전 점검",
    description: "용량/권한/보안 정책을 검사합니다.",
    stages: ["PRECHECK"],
  },
  {
    id: "copy",
    title: "데이터 복사",
    description: "처음 복사한 뒤 바뀐 내용을 계속 따라잡습니다.",
    stages: ["COPYING", "CATCHING_UP"],
  },
  {
    id: "approve",
    title: "전환 승인",
    description: "변경분이 따라잡히면 사람이 전환을 승인합니다.",
    stages: ["READY"],
    approval: true,
  },
  {
    id: "switch",
    title: "쓰기 전환",
    description:
      "원본 쓰기를 잠깐 막고 마지막 변경까지 반영해 대상으로 넘깁니다.",
    stages: ["FENCING", "DRAINING", "SYNC_CONFIRMED", "SWITCHING"],
    pause: true,
  },
  {
    id: "verify",
    title: "검증/완료",
    description: "전환 뒤 데이터와 앱을 확인하고 원본을 정리합니다.",
    stages: ["VERIFYING", "COMPLETED"],
  },
];

const EXCEPTION_STAGES = ["ABORTED", "NEEDS_RECOVERY", "ROLLED_BACK"];

export function phaseOf(
  m: Pick<MigrationSummary, "stage" | "status">,
): PhaseId | "exception" {
  if (m.status === "pending") return "prepare";
  if (m.status === "completed") return "verify";
  if (
    m.status === "aborted" ||
    m.status === "needs_recovery" ||
    m.status === "rolled_back"
  )
    return "exception";
  const key = stageKey(m.stage);
  if (EXCEPTION_STAGES.includes(key)) return "exception";
  return PHASES.find((p) => p.stages.includes(key))?.id ?? "prepare";
}

export function flowPhases(migrations: MigrationSummary[]) {
  const phases = PHASES.map((phase) => ({
    ...phase,
    jobs: migrations.filter((m) => phaseOf(m) === phase.id),
  }));
  const exceptions = migrations.filter((m) => phaseOf(m) === "exception");
  return { phases, exceptions };
}

export type AttentionItem = {
  id: "active" | "approval" | "recovery" | "failed" | "mismatch" | "apps";
  value: number;
  label: string;
  note: string;
  tone: Tone | "none";
};

export function appProblems(environments: FleetEnvironment[]) {
  return environments.flatMap((env) =>
    env.apps
      .filter((app) => appCell(app).tone === "bad")
      .map((app) => ({ env: env.name, app: app.name })),
  );
}

export function attentionItems(
  migrations: MigrationSummary[],
  environments: FleetEnvironment[],
): AttentionItem[] {
  const count = (pick: (m: MigrationSummary) => boolean) =>
    migrations.filter(pick).length;
  const active = count(
    (m) => m.status === "running" || m.status === "waiting_approval",
  );
  const approval = count((m) => m.status === "waiting_approval");
  const recovery = count(
    (m) =>
      m.status === "needs_recovery" ||
      m.status === "aborted" ||
      m.status === "rolled_back",
  );
  const failed = migrations.reduce(
    (sum, m) => sum + m.downtime.failed_requests + m.writes.failed,
    0,
  );
  const mismatch = migrations.reduce(
    (sum, m) => sum + m.writes.mismatch + (m.verify_mismatch ?? 0),
    0,
  );
  const apps = appProblems(environments);
  return [
    {
      id: "active",
      value: active,
      label: "진행 중인 작업",
      note:
        active === 0 ? `전체 ${migrations.length}개 중` : "단계가 넘어가는 중",
      tone: active > 0 ? "running" : "none",
    },
    {
      id: "approval",
      value: approval,
      label: "승인 대기",
      note: approval > 0 ? "전환을 승인해야 넘어가요" : "기다리는 승인 없음",
      tone: approval > 0 ? "warn" : "none",
    },
    {
      id: "recovery",
      value: recovery,
      label: "중단/복구 필요",
      note: recovery > 0 ? "작업을 열어 원인을 확인하세요" : "멈춘 작업 없음",
      tone: recovery > 0 ? "bad" : "none",
    },
    {
      id: "failed",
      value: failed,
      label: "실패한 쓰기",
      note: "전환 중 사용자 요청 실패",
      tone: failed > 0 ? "bad" : "none",
    },
    {
      id: "mismatch",
      value: mismatch,
      label: "데이터 불일치",
      note: "원본/대상 비교 결과",
      tone: mismatch > 0 ? "bad" : "none",
    },
    {
      id: "apps",
      value: apps.length,
      label: "비정상 앱",
      note:
        apps.length > 0
          ? apps
              .slice(0, 2)
              .map((a) => a.app)
              .join(", ")
          : "모든 앱 정상",
      tone: apps.length > 0 ? "bad" : "none",
    },
  ];
}

/** 6묶음 중 몇 번째에 있나(0부터). 예외 상태는 멈춘 단계 그대로. */
export function phaseIndex(m: Pick<MigrationSummary, "stage" | "status">) {
  const id = phaseOf(m);
  if (id === "exception") {
    const key = stageKey(m.stage);
    const at = PHASES.findIndex((p) => p.stages.includes(key));
    return at >= 0 ? at : 0;
  }
  return PHASES.findIndex((p) => p.id === id);
}

export type PhaseFact = { label: string; value: string; tone?: Tone };

/**
 * 이전 흐름 위젯: 단계마다 지금 있는 작업, 그 단계까지 온 작업(흐름 띠 두께), 단계 지표.
 */
export function flowOverview(
  migrations: MigrationSummary[],
  environments: FleetEnvironment[],
) {
  const total = migrations.length;
  const at = migrations.map((m) => ({
    m,
    i: phaseIndex(m),
    done: m.status === "completed",
  }));
  const sum = (
    list: MigrationSummary[],
    pick: (m: MigrationSummary) => number,
  ) => list.reduce((n, m) => n + pick(m), 0);
  const fmt = (n: number) => n.toLocaleString("ko-KR");
  return PHASES.map((phase, index) => {
    const jobs = at
      .filter((x) => x.i === index && !(x.done && phase.id !== "verify"))
      .map((x) => x.m);
    const reached = at.filter((x) => x.done || x.i >= index).length;
    const facts: PhaseFact[] = [];
    if (phase.id === "prepare") {
      const connected = environments.filter((e) => e.connected).length;
      facts.push({
        label: "연결된 환경",
        value: `${connected}/${environments.length}`,
      });
      facts.push({ label: "등록된 작업", value: fmt(total) });
    } else if (phase.id === "precheck") {
      facts.push({ label: "점검 항목", value: "용량/권한/정책" });
    } else if (phase.id === "copy") {
      const source = sum(jobs, (m) => m.writes.committed_source);
      const target = sum(jobs, (m) => m.writes.committed_target);
      facts.push({
        label: "원본 쓰기",
        value: jobs.length ? fmt(source) : "-",
      });
      facts.push({
        label: "대상 반영",
        value:
          jobs.length && source > 0
            ? `${Math.min(100, (target / source) * 100).toFixed(1)}%`
            : "-",
      });
    } else if (phase.id === "approve") {
      facts.push({
        label: "승인 대기",
        value: fmt(jobs.filter((m) => m.status === "waiting_approval").length),
        tone: jobs.some((m) => m.status === "waiting_approval")
          ? "warn"
          : undefined,
      });
    } else if (phase.id === "switch") {
      const pauses = migrations
        .map((m) => m.downtime.write_pause_seconds)
        .filter((s): s is number => s !== null);
      facts.push({
        label: "최대 쓰기 중단",
        value: pauses.length ? `${Math.max(...pauses).toFixed(1)}초` : "-",
      });
      const failed = sum(migrations, (m) => m.downtime.failed_requests);
      facts.push({
        label: "실패한 요청",
        value: fmt(failed),
        tone: failed > 0 ? "bad" : undefined,
      });
    } else {
      const mismatch = sum(
        migrations,
        (m) => m.writes.mismatch + (m.verify_mismatch ?? 0),
      );
      facts.push({
        label: "완료",
        value: fmt(migrations.filter((m) => m.status === "completed").length),
      });
      facts.push({
        label: "데이터 불일치",
        value: fmt(mismatch),
        tone: mismatch > 0 ? "bad" : undefined,
      });
    }
    return { phase, index, jobs, reached, total, facts };
  });
}
