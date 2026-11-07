import assert from "node:assert/strict";
import test from "node:test";
import { taskRequest } from "../src/features/prepare/taskRoute.ts";

test("같은 환경을 작업마다 다른 방향으로 사용할 수 있다", () => {
  const registered = {
    id: "connected-1",
    name: "연결 환경",
    provider: "gcp",
  } as const;
  const aws = { id: "aws", name: "AWS 환경", provider: "aws" } as const;
  assert.deepEqual(taskRequest(registered, aws, ["app"]), {
    source_env: "connected-1",
    source_provider: "gcp",
    target_env: "aws",
    target_provider: "aws",
    apps: ["app"],
  });
  assert.deepEqual(taskRequest(aws, registered, ["another-app"]), {
    source_env: "aws",
    source_provider: "aws",
    target_env: "connected-1",
    target_provider: "gcp",
    apps: ["another-app"],
  });
});
