import assert from "node:assert/strict";
import { test } from "node:test";
import type { Fleet, MigrationSummary } from "../src/api/migrations.ts";
import {
  carriedNotice,
  carryNotice,
  chosenIds,
  confirmText,
  deletableIds,
  deletionText,
  dropNotice,
  headerCheck,
  rowCheck,
  showsChecks,
  skippedText,
  toggleAll,
  toggleTask,
  withoutDeleted,
} from "../src/features/migrations/deletion.ts";

type Kind = "open" | "busy" | "static";

function job(job_id: string, kind: Kind = "open"): MigrationSummary {
  return {
    job_id,
    direction: "forward",
    source_env: "onprem",
    target_env: "aws",
    data_source: "onprem",
    data_target: "aws",
    stage: null,
    status: kind === "busy" ? "running" : "pending",
    stage_index: 0,
    stage_count: 10,
    updated_at: null,
    writes: {
      committed_source: 0,
      committed_target: 0,
      failed: 0,
      rejected: 0,
      mismatch: 0,
    },
    downtime: {
      write_pause_seconds: null,
      failed_requests: 0,
      max_latency_seconds: null,
      p95_latency_seconds: null,
    },
    verify_mismatch: null,
    registered: kind !== "static",
    can_delete: kind === "open",
  };
}

const ROWS = [
  job("onprem_to_aws_001"),
  job("onprem_to_aws_002", "busy"),
  job("forward_001", "static"),
  job("onprem_to_aws_003"),
];

function fleet(migrations: MigrationSummary[], can_delete = true): Fleet {
  return {
    generated_at: "2026-11-08T00:00:00Z",
    source: "simulated",
    environments: [],
    migrations,
    can_delete,
  };
}

test("only rows the server allows can be picked", () => {
  assert.deepEqual(deletableIds(ROWS), [
    "onprem_to_aws_001",
    "onprem_to_aws_003",
  ]);
  const picked = new Set([
    "forward_001",
    "onprem_to_aws_003",
    "onprem_to_aws_002",
    "onprem_to_aws_001",
  ]);
  assert.deepEqual(chosenIds(picked, ROWS), [
    "onprem_to_aws_001",
    "onprem_to_aws_003",
  ]);
});

test("static tasks get no checkbox and running ones a locked one", () => {
  assert.deepEqual(ROWS.map(rowCheck), ["open", "locked", "none", "open"]);
});

test("the checkbox column needs permission and a registered task in view", () => {
  assert.equal(showsChecks(fleet(ROWS), ROWS), true);
  assert.equal(showsChecks(fleet(ROWS, false), ROWS), false);
  const statics = [job("forward_001", "static")];
  assert.equal(showsChecks(fleet(statics), statics), false);
  const busy = [job("onprem_to_aws_002", "busy")];
  assert.equal(showsChecks(fleet(busy), busy), true);
});

test("selection drops rows that are no longer visible", () => {
  const picked = new Set(["onprem_to_aws_001", "onprem_to_aws_003"]);
  assert.deepEqual(chosenIds(picked, [job("onprem_to_aws_003")]), [
    "onprem_to_aws_003",
  ]);
  assert.deepEqual(chosenIds(picked, []), []);
});

test("a row checkbox toggles one id without touching the others", () => {
  const picked = toggleTask(new Set(["a_001"]), "b_001");
  assert.deepEqual([...picked].sort(), ["a_001", "b_001"]);
  assert.deepEqual([...toggleTask(picked, "a_001")], ["b_001"]);
});

test("the header checkbox selects every visible deletable row, then clears them", () => {
  const all = toggleAll(new Set(), ROWS);
  assert.deepEqual([...all].sort(), ["onprem_to_aws_001", "onprem_to_aws_003"]);
  assert.equal(headerCheck(all, ROWS), "all");
  assert.deepEqual([...toggleAll(all, ROWS)], []);

  const some = new Set(["onprem_to_aws_001"]);
  assert.equal(headerCheck(some, ROWS), "some");
  assert.deepEqual([...toggleAll(some, ROWS)].sort(), [
    "onprem_to_aws_001",
    "onprem_to_aws_003",
  ]);
  assert.equal(headerCheck(new Set(), ROWS), "none");
});

test("the header checkbox keeps picks from rows the filter hides", () => {
  const hidden = new Set(["gcp_to_aws_001", "onprem_to_aws_001"]);
  const cleared = toggleAll(toggleAll(hidden, ROWS), ROWS);
  assert.deepEqual([...cleared], ["gcp_to_aws_001"]);
});

test("with nothing deletable the header stays empty", () => {
  const busy = [job("onprem_to_aws_002", "busy"), job("forward_001", "static")];
  assert.equal(headerCheck(new Set(), busy), "none");
  assert.deepEqual([...toggleAll(new Set(), busy)], []);
  assert.equal(headerCheck(new Set(["onprem_to_aws_002"]), busy), "none");
});

test("confirm text says the count and that it cannot be undone", () => {
  assert.equal(
    confirmText(3),
    "작업 3개를 지워요. 목록과 상세 화면에서 사라지고 되돌릴 수 없어요.",
  );
});

test("skipped tasks are grouped by reason in one sentence", () => {
  assert.equal(skippedText([]), null);
  assert.equal(
    skippedText([
      { job_id: "a_001", reason: "진행 중인 작업" },
      { job_id: "forward_001", reason: "설정 파일 작업" },
      { job_id: "b_001", reason: "진행 중인 작업" },
    ]),
    "진행 중인 작업 2개, 설정 파일 작업 1개는 지우지 않았어요.",
  );
});

test("result text joins what was deleted and what was skipped", () => {
  assert.equal(
    deletionText({ deleted: ["a_001", "b_001"], skipped: [] }),
    "작업 2개를 지웠어요.",
  );
  assert.equal(
    deletionText({
      deleted: ["a_001"],
      skipped: [{ job_id: "x_001", reason: "없는 작업" }],
    }),
    "작업 1개를 지웠어요. 없는 작업 1개는 지우지 않았어요.",
  );
  assert.equal(
    deletionText({
      deleted: [],
      skipped: [{ job_id: "a_001", reason: "진행 중인 작업" }],
    }),
    "진행 중인 작업 1개는 지우지 않았어요.",
  );
});

test("copy follows the screen rules", () => {
  const texts = [
    confirmText(1),
    deletionText({
      deleted: ["a"],
      skipped: [{ job_id: "b", reason: "진행 중인 작업" }],
    }),
  ];
  for (const text of texts) {
    assert.doesNotMatch(text, /[·→←]|습니다/);
    assert.match(text, /요\.$/);
  }
});

test("deleted tasks leave the cached fleet at once", () => {
  const before = fleet(ROWS);
  const after = withoutDeleted(before, ["onprem_to_aws_001", "missing_001"]);
  assert.deepEqual(
    after.migrations.map((m) => m.job_id),
    ["onprem_to_aws_002", "forward_001", "onprem_to_aws_003"],
  );
  assert.equal(before.migrations.length, 4);
  assert.equal(withoutDeleted(before, []), before);
});

test("the overview reads a carried notice until it drops it", () => {
  dropNotice();
  assert.equal(carriedNotice(), null);
  carryNotice("작업 1개를 지웠어요.");
  assert.equal(carriedNotice(), "작업 1개를 지웠어요.");
  assert.equal(carriedNotice(), "작업 1개를 지웠어요.");
  dropNotice();
  assert.equal(carriedNotice(), null);
});
