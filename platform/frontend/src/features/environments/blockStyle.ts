import type { EnvComponent } from "../../api/migrations.ts";
import type { GroupRect } from "./canvasLayout.ts";
import { shade } from "./iso.ts";

export type BlockPaint = { top: string; height: number };

const DATA_KINDS = ["postgres", "kafka", "object_storage", "vector_db"];

// 데이터 저장소는 차트 범주색(종류 구분), 플랫폼 도구는 인디고 톤 회색, 앱은 흰색, 이전 도구는 브랜드 인디고.
const TOPS: Record<string, string> = {
  postgres: "#2784d5",
  kafka: "#dd6f23",
  object_storage: "#009b8f",
  vector_db: "#c34189",
  cloudflared: "#6b7684",
  ingress: "#6b7684",
  load_balancer: "#6b7684",
  gitops: "#d1d6db",
  rollout: "#d1d6db",
  monitoring: "#d1d6db",
  secrets: "#d1d6db",
  certificates: "#d1d6db",
  registry: "#d1d6db",
  platform: "#d1d6db",
  cache: "#918a21",
  app: "#f3f4fd",
  migration: "#6153db",
  network: "#333d4b",
};

export const UNKNOWN_TOP = "#e5e8eb";

/** 구성 요소 종류의 대표색(3D 블록 윗면과 같음). 보안 영향 경로 등 다른 그림에서도 같은 색을 쓴다. */
export function kindColor(kind: string) {
  return TOPS[kind] ?? "#8b95a1";
}

export function blockPaint(
  component: EnvComponent,
  muted: boolean,
): BlockPaint {
  const base = TOPS[component.kind] ?? "#8b95a1";
  return {
    top: muted ? UNKNOWN_TOP : base,
    height: DATA_KINDS.includes(component.kind)
      ? 26
      : component.kind === "app"
        ? 16
        : 20,
  };
}

export type ContainerPaint = {
  stroke: string;
  fill: string;
  dash: string | undefined;
  ink: string;
};

/** 경계 단계(Cloudcraft 그룹 기준처럼 단계마다 색 하나). */
export type BoundaryLevel =
  "account" | "region" | "vpc" | "subnet" | "cluster" | "namespace";

export const LEVEL_ORDER: BoundaryLevel[] = [
  "account",
  "region",
  "vpc",
  "subnet",
  "cluster",
  "namespace",
];

export const LEVEL_LABELS: Record<BoundaryLevel, string> = {
  account: "계정/사이트",
  region: "리전",
  vpc: "VPC",
  subnet: "서브넷",
  cluster: "클러스터",
  namespace: "네임스페이스",
};

/** 단계 색: 경계선, 경계 이름, 범례, 패널 배치 경로가 같은 색을 쓴다. */
export const LEVEL_COLORS: Record<BoundaryLevel, string> = {
  account: "#4e5968",
  region: "#8a63d2",
  vpc: "#17946a",
  subnet: "#2784d5",
  cluster: "#c27a0a",
  namespace: "#8b95a1",
};

export function levelOf(label: string): BoundaryLevel {
  if (
    label.startsWith("사이트") ||
    label.startsWith("AWS 계정") ||
    label.startsWith("GCP 프로젝트")
  )
    return "account";
  if (label.startsWith("리전")) return "region";
  if (label.startsWith("VPC")) return "vpc";
  if (label.includes("서브넷")) return "subnet";
  if (label.startsWith("ns ")) return "namespace";
  return "cluster";
}

function tint(hex: string, alpha: number) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

export function containerPaint(
  group: Pick<GroupRect, "kind" | "label">,
): ContainerPaint {
  const level = levelOf(group.label);
  const stroke = LEVEL_COLORS[level];
  const fill: Record<BoundaryLevel, number> = {
    account: 0.03,
    region: 0.02,
    vpc: 0.05,
    subnet: group.label.includes("public") ? 0.1 : 0.07,
    cluster: 0.05,
    namespace: 0.08,
  };
  return {
    stroke,
    fill: tint(stroke, fill[level]),
    dash:
      level === "region" ? "8 5" : level === "namespace" ? "5 4" : undefined,
    ink: shade(stroke, 0.8),
  };
}
