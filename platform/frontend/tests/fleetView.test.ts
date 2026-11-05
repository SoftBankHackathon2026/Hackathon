import assert from "node:assert/strict";
import { test } from "node:test";
import type { FleetEnvironment } from "../src/api/migrations.ts";
import {
  envName,
  appRows,
  envShortName,
} from "../src/features/migrations/fleetView.ts";

function env(id: string, role: FleetEnvironment["role"]): FleetEnvironment {
  return {
    id,
    name: `name-${id}`,
    kind: role === "legacy" ? "k3s" : "eks",
    provider: role === "legacy" ? "onprem" : "aws",
    region: "r",
    role,
    connected: true,
    observed_at: "2026-11-05T00:00:00Z",
    apps: [],
    data_services: [],
  };
}

test("envName falls back to the id", () => {
  assert.equal(envName([env("k3s", "legacy")], "k3s"), "name-k3s");
  assert.equal(envName([], "zzz"), "zzz");
});

test("envShortName drops the region and falls back to the id", () => {
  assert.equal(envShortName([env("k3s", "legacy")], "k3s"), "온프렘 k3s");
  assert.equal(envShortName([env("eks", "target")], "eks"), "AWS EKS");
  assert.equal(envShortName([], "zzz"), "zzz");
});

test("appRows lists each app once in first seen order", () => {
  const withApps = (id: string, names: string[]) =>
    ({
      ...env(id, "legacy"),
      apps: names.map((name) => ({ name })),
    }) as FleetEnvironment;
  assert.deepEqual(
    appRows([withApps("a", ["x", "y"]), withApps("b", ["y", "z"])]),
    ["x", "y", "z"],
  );
});
