import type { EnvComponent } from "../../api/migrations.ts";

export type Glyph =
  | "tunnel"
  | "ingress"
  | "balancer"
  | "gitops"
  | "monitoring"
  | "key"
  | "app"
  | "worker"
  | "connector"
  | "policy"
  | "database"
  | "stream"
  | "bucket"
  | "vector"
  | "network"
  | "box";

function circle(cx: number, cy: number, r: number) {
  return `M${cx - r} ${cy}a${r} ${r} 0 1 0 ${2 * r} 0a${r} ${r} 0 1 0 ${-2 * r} 0`;
}

export const GLYPH_PATHS: Record<Glyph, string> = {
  tunnel: "M3 12h18 M7 8l-4 4 4 4 M17 8l4 4-4 4",
  ingress: "M3 12h11 M10 7l5 5-5 5 M19 5v14",
  balancer: `${circle(12, 5, 2)} ${circle(5, 19, 2)} ${circle(19, 19, 2)} M12 7v4 M12 11l-6 6 M12 11l6 6`,
  gitops: `${circle(6, 6, 2.5)} ${circle(6, 18, 2.5)} ${circle(18, 8, 2.5)} M6 8.5v7 M18 10.5c0 4-6 3-12 5`,
  monitoring: "M3 4v16h18 M6 15l4-5 3 3 5-7",
  key: `${circle(8, 12, 3.5)} M11.5 12H21 M18 12v3 M15 12v2`,
  app: "M4 5h16v14H4z M4 9h16",
  worker: `${circle(12, 12, 3)} M12 3v3 M12 18v3 M3 12h3 M18 12h3`,
  connector: "M3 12h5 M16 12h5 M8 9h8v6H8z",
  policy: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z M9 12l2 2 4-4",
  database:
    "M5 6c0-1.7 3.1-3 7-3s7 1.3 7 3-3.1 3-7 3-7-1.3-7-3z M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6 M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3",
  stream: "M3 7h12 M3 12h18 M3 17h12 M18 9l3 3-3 3",
  bucket: "M5 8h14l-1.5 11h-11z M5 8c0-1.7 3.1-3 7-3s7 1.3 7 3",
  vector: "M12 3l8 5v8l-8 5-8-5V8z M12 12l8-4 M12 12v9 M12 12L4 8",
  network: `${circle(12, 12, 9)} M3 12h18 M12 3c3 3 3 15 0 18 M12 3c-3 3-3 15 0 18`,
  box: "M4 7l8-4 8 4v10l-8 4-8-4z M4 7l8 4 8-4 M12 11v10",
};

export function glyphFor(component: Pick<EnvComponent, "id" | "kind">): Glyph {
  const id = component.id.toLowerCase();
  switch (component.kind) {
    case "cloudflared":
      return "tunnel";
    case "ingress":
      return "ingress";
    case "load_balancer":
      return "balancer";
    case "gitops":
    case "rollout":
      return "gitops";
    case "monitoring":
      return "monitoring";
    case "secrets":
      return "key";
    case "app":
      return id.includes("worker") ? "worker" : "app";
    case "migration":
      if (id.includes("policy")) return "policy";
      if (id.includes("worker")) return "worker";
      return "connector";
    case "postgres":
      return "database";
    case "kafka":
      return "stream";
    case "object_storage":
      return "bucket";
    case "vector_db":
      return "vector";
    case "network":
      return "network";
    default:
      return "box";
  }
}
