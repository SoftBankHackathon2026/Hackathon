import assert from "node:assert/strict";
import test from "node:test";
import type { ComponentPod, EnvComponent } from "../src/api/migrations.ts";
import {
  podAge,
  podCells,
  podLabel,
  podSummary,
  podTone,
} from "../src/features/environments/podModel.ts";

function c(over: Partial<EnvComponent>): EnvComponent {
  return {
    id: "x",
    name: "x",
    layer: "app",
    kind: "app",
    managed: false,
    status: "healthy",
    detail: "",
    depends_on: [],
    ...over,
  };
}

function pod(over: Partial<ComponentPod>): ComponentPod {
  return {
    name: "p",
    phase: "Running",
    ready: true,
    restarts: 0,
    node: "node-1",
    started_at: null,
    containers: [],
    ...over,
  };
}

test("podTone and podLabel read phase, readiness and reason", () => {
  assert.equal(podTone(pod({})), "ok");
  assert.equal(podLabel(pod({})), "실행 중");
  assert.equal(podTone(pod({ phase: "Pending", ready: false })), "warn");
  assert.equal(podLabel(pod({ phase: "Pending", ready: false })), "시작 중");
  const crash = pod({ ready: false, reason: "CrashLoopBackOff" });
  assert.equal(podTone(crash), "bad");
  assert.equal(podLabel(crash), "재시작 반복(CrashLoopBackOff)");
  assert.equal(podLabel(pod({ reason: "SomethingNew" })), "SomethingNew");
  assert.equal(podTone(pod({ phase: "Succeeded", ready: false })), "idle");
});

test("podSummary prefers pod list and falls back to detail", () => {
  assert.deepEqual(
    podSummary(
      c({
        pods: [
          pod({ name: "a" }),
          pod({ name: "b", ready: false, restarts: 3 }),
        ],
      }),
    ),
    { ready: 1, total: 2, restarts: 3, source: "pods" },
  );
  assert.deepEqual(podSummary(c({ detail: "워크로드 6개, 준비 5/5" })), {
    ready: 5,
    total: 5,
    restarts: null,
    source: "detail",
  });
  assert.equal(
    podSummary(
      c({ managed: true, detail: "관리형 서비스 상태는 아직 수집하지 않음" }),
    ),
    null,
  );
});

test("podCells puts problem pods first and caps the count", () => {
  const { cells, hidden } = podCells(
    c({ detail: "준비 1/3", status: "degraded" }),
    2,
  );
  assert.deepEqual(
    cells.map((x) => x.tone),
    ["warn", "warn"],
  );
  assert.equal(hidden, 1);
});

test("podAge prints a short age", () => {
  const now = Date.parse("2026-11-06T12:00:00Z");
  assert.equal(podAge("2026-11-06T11:30:00Z", now), "30분");
  assert.equal(podAge("2026-11-05T12:00:00Z", now), "24시간");
  assert.equal(podAge("2026-11-01T12:00:00Z", now), "5일");
  assert.equal(podAge(null, now), null);
});
