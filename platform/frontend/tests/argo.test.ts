import assert from "node:assert/strict";
import { test } from "node:test";
import type { FleetApp } from "../src/api/migrations.ts";
import { appCell } from "../src/features/migrations/argo.ts";

const app = (sync: FleetApp["sync"], health: FleetApp["health"]) =>
  ({ sync, health }) as FleetApp;

test("appCell says what a coworker would say", () => {
  assert.deepEqual(appCell(app("Synced", "Healthy")), {
    tone: "ok",
    label: "정상",
  });
  assert.deepEqual(appCell(app("Unknown", "Unknown")), {
    tone: "idle",
    label: "상태 미수집",
  });
  assert.deepEqual(appCell(undefined), { tone: "idle", label: "-" });
  assert.deepEqual(appCell(app("OutOfSync", "Healthy")), {
    tone: "warn",
    label: "저장소 설정과 다름",
  });
  assert.deepEqual(appCell(app("Synced", "Degraded")), {
    tone: "bad",
    label: "문제 있음",
  });
  assert.deepEqual(appCell(app("Synced", "Progressing")), {
    tone: "running",
    label: "배포 중",
  });
  assert.equal(appCell(app("Synced", "Missing")).tone, "bad");
});
