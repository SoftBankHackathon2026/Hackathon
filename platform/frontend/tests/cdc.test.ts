import assert from "node:assert/strict";
import { test } from "node:test";
import type { MigrationDetail } from "../src/api/migrations.ts";
import { cdcNodes } from "../src/features/migrations/cdc.ts";

const names = { source: "온프렘 k3s", target: "AWS EKS" };
const counts = { source: 2500, target: 12 };

function detail(status: string, connectors: unknown[]): MigrationDetail {
  return {
    summary: {
      job_id: "forward_001",
      status,
      data_source: "onprem",
      data_target: "aws",
    },
    connectors,
  } as unknown as MigrationDetail;
}

test("topology runs source db, source connector, topic, sink connector, target db", () => {
  const nodes = cdcNodes(
    detail("running", [
      {
        name: "s",
        role: "source",
        state: "RUNNING",
        tasks_running: 1,
        tasks_total: 1,
      },
      {
        name: "k",
        role: "sink",
        state: "FAILED",
        tasks_running: 0,
        tasks_total: 1,
      },
    ]),
    names,
    counts,
  );
  assert.deepEqual(
    nodes.map((n) => n.key),
    ["source-db", "source", "topic", "sink", "target-db"],
  );
  assert.equal(nodes[0].metric, "쓰기 2,500건");
  assert.equal(nodes[1].flag, null);
  assert.equal(nodes[3].flag, "커넥터 실패");
  assert.equal(nodes[3].tone, "bad");
  assert.equal(nodes[2].name, "forward_001");
});

test("missing connectors are grey after cutover and red while running", () => {
  const done = cdcNodes(detail("completed", []), names, counts);
  assert.equal(done[1].name, "삭제됨");
  assert.equal(done[1].tone, "idle");
  const running = cdcNodes(detail("running", []), names, counts);
  assert.equal(running[3].tone, "bad");
  assert.equal(running[3].flag, "커넥터 없음");
  assert.equal(cdcNodes(detail("pending", []), names, counts)[1].tone, "idle");
});
