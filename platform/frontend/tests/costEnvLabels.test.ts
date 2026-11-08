import assert from "node:assert/strict";
import { test } from "node:test";
import type { CostOverview } from "../src/api/costs.ts";
import type { FleetEnvironment } from "../src/api/migrations.ts";
import { envLabels } from "../src/features/costs/costView.ts";

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
    name: "연결 해제된 환경",
    provider: undefined,
  });
  assert.equal(label("legacy-env").name, "legacy-env");
  assert.equal(
    envLabels(overview, undefined)("connected-abc").name,
    "연결 해제된 환경",
  );
});
