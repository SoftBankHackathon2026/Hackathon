import type {
  ComponentLayer,
  ComponentStatus,
  EnvComponent,
  FleetEnvironment,
} from "../../api/migrations.ts";
import type { CardLine } from "../../components/SignalCard.tsx";
import { formatAgo } from "../../lib/format.ts";
import { KIND_LABELS as PLATFORM_LABELS } from "../migrations/fleetView.ts";
import type { Tone } from "../migrations/stages.ts";

export const LAYER_ORDER: ComponentLayer[] = [
  "entry",
  "platform",
  "app",
  "migration",
  "data",
  "link",
];

export const LAYER_LABELS: Record<ComponentLayer, string> = {
  entry: "진입점",
  platform: "배포·운영",
  app: "앱",
  migration: "이전 도구",
  data: "데이터",
  link: "환경 간 연결",
};

export const COMPONENT_STATUS_LABELS: Record<ComponentStatus, string> = {
  healthy: "정상",
  degraded: "일부만 준비됨",
  down: "중단",
  unknown: "상태 미수집",
};

const KIND_LABELS: Record<string, string> = {
  cloudflared: "터널",
  ingress: "인그레스",
  gitops: "GitOps",
  rollout: "롤아웃",
  monitoring: "모니터링",
  secrets: "시크릿",
  certificates: "인증서",
  registry: "이미지 레지스트리",
  platform: "플랫폼",
  cache: "캐시",
  app: "앱",
  migration: "이전 도구",
  postgres: "PostgreSQL",
  kafka: "Kafka",
  object_storage: "오브젝트 스토리지",
  vector_db: "벡터 DB",
  network: "네트워크",
  load_balancer: "로드 밸런서",
};

export function kindLabel(kind: string) {
  return KIND_LABELS[kind] ?? kind;
}

export function componentTone(status: ComponentStatus): Tone {
  switch (status) {
    case "healthy":
      return "ok";
    case "degraded":
      return "warn";
    case "down":
      return "bad";
    default:
      return "idle";
  }
}

export function envSummary(
  env: Pick<FleetEnvironment, "components" | "issues">,
) {
  const count = (status: ComponentStatus) =>
    env.components.filter((c) => c.status === status).length;
  return {
    total: env.components.length,
    healthy: count("healthy"),
    degraded: count("degraded"),
    down: count("down"),
    unknown: count("unknown"),
    critical: env.issues.filter((i) => i.severity === "critical").length,
    warning: env.issues.filter((i) => i.severity === "warning").length,
  };
}

export function groupByLayer(components: EnvComponent[]) {
  return LAYER_ORDER.map((layer) => ({
    layer,
    components: components.filter((c) => c.layer === layer),
  })).filter((group) => group.components.length > 0);
}

export function dependents(components: EnvComponent[], id: string) {
  return components.filter((c) => c.depends_on.includes(id));
}

export function nodeDetail(component: EnvComponent) {
  if (component.status === "unknown") return "상태 미수집";
  if (component.status === "healthy") return detailText(component.detail);
  return `${COMPONENT_STATUS_LABELS[component.status]} · ${detailText(component.detail)}`;
}

export function shortName(environments: FleetEnvironment[], id: string) {
  return environments.find((e) => e.id === id)?.name ?? id;
}

export function envSummaryLine(
  env: Pick<FleetEnvironment, "components" | "issues">,
): { line: CardLine; note: string } {
  const summary = envSummary(env);
  const trouble = summary.degraded + summary.down;
  const first = env.issues[0];
  const note = (() => {
    if (summary.total === 0) return "구성 요소 정보가 없어요";
    if (summary.unknown === summary.total)
      return `구성 요소 ${summary.total}개 모두 상태를 받지 못했어요`;
    if (summary.unknown > 0)
      return `구성 요소 ${summary.total}개 중 ${summary.healthy}개 정상, ${summary.unknown}개는 상태 미수집`;
    return `구성 요소 ${summary.total}개 중 ${summary.healthy}개 정상`;
  })();
  if (summary.critical > 0 && first)
    return { line: { tone: "bad", text: first.title }, note };
  if (trouble > 0)
    return {
      line: { tone: "bad", text: `${trouble}개 구성 요소에 문제가 있어요` },
      note,
    };
  if (first) return { line: { tone: "warn", text: first.title }, note };
  return { line: { tone: "ok", text: "정상 운영 중" }, note };
}

export function detailText(detail: string) {
  if (!detail) return "";
  const ready = /^준비 (\d+)\/(\d+)$/.exec(detail);
  if (ready) return `복제본 ${ready[2]}개 중 ${ready[1]}개 준비됨`;
  if (detail === "Synced · Healthy") return "정상 동작 중";
  if (detail === "Unknown · Unknown") return "상태 미수집";
  return detail
    .replace(
      "관리형 서비스 상태는 아직 수집하지 않음",
      "관리형 서비스라 상태를 수집하지 않아요",
    )
    .replace("상태를 읽지 못함", "상태를 읽지 못했어요");
}

export type RailRow = { label: string; value: string; tone?: Tone };

export function envRailRows(
  env: FleetEnvironment,
  now = Date.now(),
): RailRow[] {
  const summary = envSummary(env);
  const { line } = envSummaryLine(env);
  const kinds: string[] = [];
  for (const c of env.components)
    if (c.layer === "data" && !kinds.includes(kindLabel(c.kind)))
      kinds.push(kindLabel(c.kind));
  return [
    {
      label: "상태",
      value: line.text,
      tone: line.tone === "none" ? undefined : line.tone,
    },
    { label: "구성 요소", value: `${summary.healthy} / ${summary.total} 정상` },
    { label: "플랫폼", value: `${PLATFORM_LABELS[env.kind]} · ${env.region}` },
    {
      label: "앱",
      value: String(env.components.filter((c) => c.layer === "app").length),
    },
    { label: "데이터", value: kinds.length > 0 ? kinds.join(", ") : "-" },
    { label: "관측", value: formatAgo(env.observed_at, now) },
  ];
}

export type ComponentRow = {
  envId: string;
  envName: string;
  provider: string;
  component: EnvComponent;
  placement: string;
};

export function componentRows(
  environments: FleetEnvironment[],
): ComponentRow[] {
  return environments.flatMap((env) =>
    env.components.map((component) => ({
      envId: env.id,
      envName: env.name,
      provider: env.provider,
      component,
      placement: [
        ...(env.boundary ?? []).slice(1),
        ...(component.placement ?? []),
      ]
        .slice(-2)
        .join(" › "),
    })),
  );
}

export function filterComponentRows(
  rows: ComponentRow[],
  envId: string | "all",
  query: string,
) {
  const q = query.trim().toLowerCase();
  return rows.filter(
    (row) =>
      (envId === "all" || row.envId === envId) &&
      (q === "" ||
        `${row.component.name} ${row.component.id} ${row.component.kind} ${row.placement}`
          .toLowerCase()
          .includes(q)),
  );
}
