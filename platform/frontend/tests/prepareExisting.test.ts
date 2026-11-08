import assert from "node:assert/strict";
import { test } from "node:test";
import type { FleetEnvironment } from "../src/api/migrations.ts";
import { existingEnvironments } from "../src/features/prepare/model.ts";

function env(
  id: string,
  extra: Partial<FleetEnvironment> = {},
): FleetEnvironment {
  return {
    id,
    name: id,
    kind: "k3s",
    provider: "onprem",
    region: "seoul",
    role: "legacy",
    connected: true,
    observed_at: null,
    apps: [],
    data_services: [],
    components: [],
    issues: [],
    ...extra,
  };
}

test("existing environments list the built-in ones and skip directly registered", () => {
  const got = existingEnvironments([
    env("onprem"),
    env("aws", { provider: "aws", kind: "eks", role: "target" }),
    env("gcp", {
      provider: "gcp",
      kind: "gke",
      role: "target",
      connected: false,
    }),
    env("connected-abc", { registered: true }),
  ]);
  assert.deepEqual(got, [
    {
      id: "onprem",
      name: "onprem",
      detail: "온프레미스",
      label: "연결됨",
      tone: "ok",
    },
    {
      id: "aws",
      name: "aws",
      detail: "AWS",
      label: "연결됨",
      tone: "ok",
    },
    {
      id: "gcp",
      name: "gcp",
      detail: "GCP",
      label: "상태 정보 없음",
      tone: "idle",
    },
  ]);
  assert.deepEqual(existingEnvironments(undefined), []);
});
