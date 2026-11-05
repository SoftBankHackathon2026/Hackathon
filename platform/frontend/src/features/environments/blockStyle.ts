import type { EnvComponent } from "../../api/migrations.ts";
import type { GroupRect } from "./canvasLayout.ts";
import { shade } from "./iso.ts";

export type BlockPaint = { top: string; height: number };

const DATA_KINDS = ["postgres", "kafka", "object_storage", "vector_db"];

const TOPS: Record<string, string> = {
  postgres: "#0f5c63",
  kafka: "#2b2b2b",
  object_storage: "#6fa3a7",
  vector_db: "#3f7378",
  cloudflared: "#6b6b6b",
  ingress: "#6b6b6b",
  load_balancer: "#6b6b6b",
  gitops: "#a9c7c9",
  rollout: "#a9c7c9",
  monitoring: "#a9c7c9",
  secrets: "#a9c7c9",
  app: "#f2f2f2",
  migration: "#14383c",
  network: "#525252",
};

export const UNKNOWN_TOP = "#d9d9d9";

export function blockPaint(
  component: EnvComponent,
  muted: boolean,
): BlockPaint {
  const base = TOPS[component.kind] ?? "#8a8a8a";
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

export function containerPaint(
  group: Pick<GroupRect, "kind" | "label">,
): ContainerPaint {
  const make = (
    stroke: string,
    fill: string,
    dash?: string,
  ): ContainerPaint => ({
    stroke,
    fill,
    dash,
    ink: shade(stroke, 0.72),
  });
  switch (group.kind) {
    case "account":
      return group.label.startsWith("사이트")
        ? make("#8a8a8a", "rgba(138,138,138,0.07)")
        : make("#3a3a3a", "rgba(58,58,58,0.04)");
    case "network":
      return group.label.startsWith("리전")
        ? make("#4f7f86", "rgba(79,127,134,0.03)", "7 5")
        : make("#6a8f7a", "rgba(106,143,122,0.07)");
    case "subnet":
      return group.label.includes("public")
        ? make("#6a8f7a", "rgba(106,143,122,0.18)")
        : make("#4f7f86", "rgba(79,127,134,0.11)");
    case "cluster":
      return make("#6b6b6b", "rgba(107,107,107,0.06)");
    default:
      return make("#7fa9ac", "rgba(127,169,172,0.10)", "5 4");
  }
}
