import assert from "node:assert/strict";
import { test } from "node:test";
import type { CostOverview } from "../src/api/costs.ts";
import type { FleetEnvironment } from "../src/api/migrations.ts";
import {
  envLabels,
  withoutDisconnected,
} from "../src/features/costs/costView.ts";

const overview = {
  environments: [
    { env_id: "aws", name: "AWS EKS oneaction (서울)", provider: "aws" },
  ],
} as unknown as CostOverview;

const fleet = [
  { id: "aws", name: "옛 이름", provider: "aws" },
  { id: "connected-abc", name: "k3s-local", provider: "onprem" },
] as unknown as FleetEnvironment[];

test("env labels prefer cost names, then registered environments, then the id", () => {
  const label = envLabels(overview, fleet);
  assert.deepEqual(label("aws"), {
    name: "AWS EKS oneaction (서울)",
    provider: "aws",
  });
  assert.deepEqual(label("connected-abc"), {
    name: "k3s-local",
    provider: "onprem",
  });
  assert.deepEqual(label("connected-zzz"), {
    name: "connected-zzz",
    provider: undefined,
  });
  assert.equal(label("legacy-env").name, "legacy-env");
  assert.equal(
    envLabels(overview, undefined)("connected-abc").name,
    "connected-abc",
  );
});

test("tasks on disconnected environments drop out of the cost view", () => {
  const costs = {
    ...overview,
    migrations: [
      { job_id: "a", source_env: "aws", target_env: "connected-abc" },
      { job_id: "b", source_env: "connected-gone", target_env: "aws" },
      { job_id: "c", source_env: "aws", target_env: "connected-gone" },
      { job_id: "d", source_env: "legacy-env", target_env: "aws" },
    ],
  } as unknown as CostOverview;
  assert.deepEqual(
    withoutDisconnected(costs, fleet).migrations.map((m) => m.job_id),
    ["a", "d"],
  );
  assert.deepEqual(
    withoutDisconnected(costs, undefined).migrations.map((m) => m.job_id),
    ["d"],
  );
  assert.equal(
    withoutDisconnected(costs, fleet).environments,
    costs.environments,
  );
});
