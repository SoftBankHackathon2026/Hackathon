import assert from "node:assert/strict";
import { test } from "node:test";
import type { AgentRun, AgentRunState } from "../src/api/agent.ts";
import {
  chosenIds,
  confirmText,
  deletableIds,
  deletionText,
  headerCheck,
  skippedText,
  toggleAll,
  toggleRun,
} from "../src/features/agent/deletion.ts";

const ACTIVE = new Set(["CHECKING", "APPLYING", "WAITING_APPROVAL"]);

function run(run_id: string, state: AgentRunState = "HANDED_OFF"): AgentRun {
  return {
    run_id,
    agent_id: "agent-a",
    job_id: "forward_001",
    state,
    created_at: "2026-11-07T00:00:00Z",
    updated_at: "2026-11-07T00:00:00Z",
    summary: { reason_codes: [], changes: [], diagnosis: null },
    can_delete: !ACTIVE.has(state),
  };
}

const ROWS = [run("run-a"), run("run-b", "CHECKING"), run("run-c")];

test("only rows the server allows can be picked", () => {
  assert.deepEqual(deletableIds(ROWS), ["run-a", "run-c"]);
  assert.deepEqual(chosenIds(new Set(["run-b", "run-c", "run-a"]), ROWS), [
    "run-a",
    "run-c",
  ]);
});

test("selection drops rows that are no longer visible", () => {
  const picked = new Set(["run-a", "run-c"]);
  assert.deepEqual(chosenIds(picked, [run("run-c")]), ["run-c"]);
  assert.deepEqual(chosenIds(picked, []), []);
});

test("a row checkbox toggles one id without touching the others", () => {
  const picked = toggleRun(new Set(["run-a"]), "run-c");
  assert.deepEqual([...picked].sort(), ["run-a", "run-c"]);
  assert.deepEqual([...toggleRun(picked, "run-a")], ["run-c"]);
});

test("the header checkbox selects every visible deletable row, then clears them", () => {
  const all = toggleAll(new Set(), ROWS);
  assert.deepEqual([...all].sort(), ["run-a", "run-c"]);
  assert.equal(headerCheck(all, ROWS), "all");
  assert.deepEqual([...toggleAll(all, ROWS)], []);

  const some = new Set(["run-a"]);
  assert.equal(headerCheck(some, ROWS), "some");
  assert.deepEqual([...toggleAll(some, ROWS)].sort(), ["run-a", "run-c"]);
  assert.equal(headerCheck(new Set(), ROWS), "none");
});

test("the header checkbox keeps picks from rows the filter hides", () => {
  const hidden = new Set(["run-z", "run-a"]);
  const cleared = toggleAll(toggleAll(hidden, ROWS), ROWS);
  assert.deepEqual([...cleared], ["run-z"]);
});

test("with nothing deletable the header stays empty", () => {
  const busy = [run("run-b", "CHECKING")];
  assert.equal(headerCheck(new Set(), busy), "none");
  assert.deepEqual([...toggleAll(new Set(), busy)], []);
  assert.equal(headerCheck(new Set(["run-b"]), busy), "none");
});

test("confirm text says the count and that the agent cannot bring it back", () => {
  assert.equal(
    confirmText(18),
    "기록 18개를 지워요. 에이전트가 다시 보내도 목록에 나오지 않아요.",
  );
});

test("skipped runs read as one sentence grouped by reason", () => {
  assert.equal(skippedText([]), null);
  assert.equal(
    skippedText([
      { run_id: "run-b", reason: "진행 중인 실행" },
      { run_id: "run-x", reason: "없는 실행" },
      { run_id: "run-d", reason: "진행 중인 실행" },
    ]),
    "진행 중인 실행 2개, 없는 실행 1개는 지우지 않았어요.",
  );
});

test("the result sentence names what was removed and what was kept", () => {
  assert.equal(
    deletionText({ deleted: ["run-a", "run-c"], skipped: [] }),
    "기록 2개를 지웠어요.",
  );
  assert.equal(
    deletionText({
      deleted: ["run-a"],
      skipped: [
        { run_id: "run-b", reason: "진행 중인 실행" },
        { run_id: "run-e", reason: "진행 중인 실행" },
      ],
    }),
    "기록 1개를 지웠어요. 진행 중인 실행 2개는 지우지 않았어요.",
  );
  assert.equal(
    deletionText({
      deleted: [],
      skipped: [{ run_id: "run-b", reason: "진행 중인 실행" }],
    }),
    "진행 중인 실행 1개는 지우지 않았어요.",
  );
});

test("copy avoids middle dots and arrows", () => {
  const texts = [
    confirmText(3),
    deletionText({
      deleted: ["run-a"],
      skipped: [{ run_id: "run-b", reason: "진행 중인 실행" }],
    }),
  ];
  for (const text of texts) assert.doesNotMatch(text, /[·→]/);
});
