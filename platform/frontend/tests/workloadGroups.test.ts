import assert from "node:assert/strict";
import { test } from "node:test";
import { classifyWorkload } from "../src/features/prepare/workloadGroups.ts";
import { appWorkloads } from "./fixtures/appWorkloads.ts";

test("groups the brief's 51 representative workloads", () => {
  const byGroup = Object.groupBy(appWorkloads, classifyWorkload);
  assert.deepEqual(
    Object.fromEntries(
      Object.entries(byGroup).map(([group, rows]) => [group, rows.length]),
    ),
    { app: 6, data: 7, migration: 9, infrastructure: 29 },
  );
  assert.equal(byGroup.app?.[0].name, "sample-app");
  assert.ok(byGroup.app?.some((item) => item.name === "review-api"));
  assert.ok(
    byGroup.data?.some((item) => item.name === "durable-ingress-redis"),
  );
});

test("handles rule priority, image references, and persistent storage", () => {
  const app = appWorkloads[0];
  const migrationImage = "ghcr.io/softbankhackathon2026/migration-test:v1";
  assert.equal(
    classifyWorkload({
      ...app,
      namespace: "monitoring",
      images: [migrationImage],
    }),
    "infrastructure",
  );
  assert.equal(
    classifyWorkload({ ...app, kind: "DaemonSet", images: [migrationImage] }),
    "infrastructure",
  );
  assert.equal(
    classifyWorkload({ ...app, name: "traffic-bot", images: ["redis"] }),
    "migration",
  );
  assert.equal(
    classifyWorkload({
      ...app,
      images: ["ghcr.io/softbankhackathon2026/ghost-agent@sha256:abc"],
    }),
    "migration",
  );
  assert.equal(
    classifyWorkload({
      ...app,
      images: [
        "registry:5000/library/redis:7@sha256:abc",
        "docker.io/library/postgres:16",
      ],
    }),
    "data",
  );
  assert.equal(
    classifyWorkload({ ...app, images: ["registry:5000/app-redis:7"] }),
    "app",
  );
  assert.equal(classifyWorkload({ ...app, images: [] }), "app");
  assert.equal(
    classifyWorkload({ ...app, kind: "StatefulSet", pvcs: ["app-data"] }),
    "data",
  );
  assert.equal(
    classifyWorkload({ ...app, kind: "StatefulSet", pvcs: [] }),
    "app",
  );
});
