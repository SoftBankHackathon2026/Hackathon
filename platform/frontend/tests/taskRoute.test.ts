import assert from "node:assert/strict";
import { test } from "node:test";
import { taskRequest } from "../src/features/prepare/taskRoute.ts";

test("출발 환경으로 등록한 연결은 작업의 원본이 된다", () => {
  assert.deepEqual(
    taskRequest(
      { id: "connected-1", role: "source", provider: "onprem" },
      "aws",
      ["a"],
    ),
    {
      source_env: "connected-1",
      source_provider: "onprem",
      target_env: "aws",
      apps: ["a"],
    },
  );
});

test("도착 환경으로 등록한 연결은 작업의 대상이 된다", () => {
  assert.deepEqual(
    taskRequest(
      { id: "connected-2", role: "target", provider: "onprem" },
      "aws",
      [],
    ),
    {
      source_env: "aws",
      target_env: "connected-2",
      target_provider: "onprem",
      apps: [],
    },
  );
});
