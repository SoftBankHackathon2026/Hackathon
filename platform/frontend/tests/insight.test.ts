import assert from "node:assert/strict";
import test from "node:test";
import type { EnvironmentCost } from "../src/api/costs.ts";
import type {
  EnvComponent,
  FleetEnvironment,
  MigrationSummary,
} from "../src/api/migrations.ts";
import {
  componentCost,
  migrationRoles,
  placementTrail,
  readiness,
} from "../src/features/environments/insight.ts";

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

test("readiness splits ready count, workloads and notes", () => {
  assert.deepEqual(readiness("워크로드 6개, 준비 5/5"), {
    ready: { ready: 5, total: 5 },
    workloads: 6,
    notes: [],
  });
  assert.deepEqual(
    readiness(
      "관리형 서비스 상태는 아직 수집하지 않음, 현재 HTTP 80 임시 모드",
    ),
    {
      ready: null,
      workloads: null,
      notes: [
        "관리형 서비스라 상태를 수집하지 않아요",
        "현재 HTTP 80 임시 모드",
      ],
    },
  );
});

test("placementTrail joins env boundary and placement with levels", () => {
  const env = { boundary: ["AWS 계정 a", "리전 r"] } as FleetEnvironment;
  const trail = placementTrail(
    env,
    c({ placement: ["VPC 10.0.0.0/16", "EKS 서브넷", "EKS one", "ns app"] }),
  );
  assert.deepEqual(
    trail.map((t) => t.level),
    ["account", "region", "vpc", "subnet", "cluster", "namespace"],
  );
});

test("componentCost matches managed services by resource name", () => {
  const envCost = {
    current: {
      lines: [
        { item: "MSK 브로커 kafka.t3.small / oneaction-review", monthly: 83 },
        { item: "RDS db.t4g.micro / oneaction-review-db", monthly: 18 },
      ],
    },
  } as unknown as EnvironmentCost;
  assert.equal(
    componentCost(c({ name: "MSK oneaction-review", managed: true }), envCost)
      ?.monthly,
    83,
  );
  assert.equal(componentCost(c({ name: "sample-app" }), envCost), null);
});

test("migrationRoles finds the source db of a job", () => {
  const env = {
    id: "onprem",
    components: [c({ id: "pg", kind: "postgres", layer: "data" })],
  } as FleetEnvironment;
  const job = {
    job_id: "forward_001",
    source_env: "onprem",
    target_env: "aws",
    data_source: "onprem",
    data_target: "aws",
  } as MigrationSummary;
  const roles = migrationRoles(env, env.components[0], [job], [env]);
  assert.deepEqual(
    roles.map((r) => r.role),
    ["원본 DB"],
  );
});
