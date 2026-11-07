import assert from "node:assert/strict";
import { test } from "node:test";
import type { MigrationDetail } from "../src/api/migrations.ts";
import { headline } from "../src/features/migrations/headline.ts";

const names = { source: "온프렘 k3s", target: "AWS EKS" };

function detail(
  over: Record<string, unknown>,
  failed = 0,
  writeFailed = 0,
): MigrationDetail {
  return {
    summary: {
      status: "completed",
      stage: "COMPLETED",
      stage_index: 9,
      stage_count: 10,
      writes: { committed_target: 2978, failed: writeFailed },
      downtime: { failed_requests: failed, write_pause_seconds: 30 },
      ...over,
    },
    verify: {
      pre: null,
      post: {
        passed: true,
        counts: { B_minus_T: 0, T_minus_B: 0, field_mismatch: 0, S_minus_T: 0 },
        source_count: 1,
        target_count: 1,
        committed_count: 1,
      },
    },
  } as unknown as MigrationDetail;
}

const text = (parts: { text: string }[]) => parts.map((p) => p.text).join("");

test("completed headline states writes, failures, pause and mismatch", () => {
  const result = headline(detail({}), names);
  assert.equal(result.label, "완료");
  assert.equal(
    text(result.parts),
    "쓰기 2,978건이 AWS EKS로 넘어갔어요. 실패한 쓰기는 없고, 쓰기 중단은 최대 30초였어요. 데이터 불일치도 없어요.",
  );
  assert.ok(result.parts.every((p) => !("bad" in p) || !p.bad));
});

test("only nonzero problem numbers are flagged", () => {
  const parts = headline(detail({}, 3), names).parts;
  assert.deepEqual(
    parts.filter((p) => p.bad).map((p) => p.text),
    ["3건"],
  );
});

test("waiting approval explains what approval starts", () => {
  const result = headline(
    detail({ status: "waiting_approval", stage: "READY", stage_index: 3 }),
    names,
  );
  assert.equal(result.label, "승인 대기");
  assert.match(text(result.parts), /승인하면 쓰기 차단부터 시작해요/);
});

test("stopped jobs name the stage and flag failures", () => {
  const result = headline(
    detail({ status: "aborted", stage: "FENCING" }, 2),
    names,
  );
  assert.equal(result.tone, "bad");
  assert.match(text(result.parts), /쓰기 차단 단계에서 멈췄어요/);
  assert.equal(result.parts.filter((p) => p.bad).length, 1);
});

test("failed writes count rejected writes and failed requests together", () => {
  const parts = headline(detail({}, 2, 5), names).parts;
  assert.deepEqual(
    parts.filter((p) => p.bad).map((p) => p.text),
    ["7건"],
  );
  const running = headline(
    detail({ status: "running", stage: "SYNCING", stage_index: 4 }, 0, 1),
    names,
  );
  assert.match(text(running.parts), /실패한 쓰기는 1건이에요/);
});
