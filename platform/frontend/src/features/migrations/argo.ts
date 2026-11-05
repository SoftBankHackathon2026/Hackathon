import type { FleetApp, HealthState, SyncState } from "../../api/migrations.ts";
import type { Tone } from "./stages.ts";

export function syncTone(sync: SyncState): Tone {
  if (sync === "Synced") return "ok";
  if (sync === "OutOfSync") return "warn";
  return "idle";
}

export function healthTone(health: HealthState): Tone {
  switch (health) {
    case "Healthy":
      return "ok";
    case "Progressing":
      return "running";
    case "Degraded":
    case "Missing":
      return "bad";
    default:
      return "idle";
  }
}

export function appCell(app: FleetApp | undefined): {
  tone: Tone;
  label: string;
} {
  if (!app) return { tone: "idle", label: "-" };
  if (app.sync === "Unknown" && app.health === "Unknown")
    return { tone: "idle", label: "상태 미수집" };
  if (app.health === "Missing")
    return { tone: "bad", label: "배포돼 있지 않음" };
  if (app.health === "Degraded") return { tone: "bad", label: "문제 있음" };
  if (app.sync === "OutOfSync")
    return { tone: "warn", label: "저장소 설정과 다름" };
  if (app.health === "Progressing")
    return { tone: "running", label: "배포 중" };
  if (app.health === "Healthy") return { tone: "ok", label: "정상" };
  return { tone: "idle", label: "상태 미수집" };
}
