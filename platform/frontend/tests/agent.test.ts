import assert from "node:assert/strict";
import { test } from "node:test";
import type { AgentRun, AgentRunState } from "../src/api/agent.ts";
import {
  approvalModeLabel,
  changeRows,
  changeText,
  filterRuns,
  gateView,
  reasonText,
  RUN_STATE_LABELS,
  runStateLabel,
  runTone,
} from "../src/features/agent/model.ts";

function run(state: AgentRunState, run_id = `run-${state}`): AgentRun {
  return {
    run_id,
    agent_id: "agent-a",
    job_id: "forward_001",
    state,
    created_at: "2026-11-07T00:00:00Z",
    updated_at: "2026-11-07T00:00:00Z",
    summary: { reason_codes: [], changes: [], diagnosis: null },
    can_delete: true,
  };
}

test("run states map to tones that flag what needs a person", () => {
  assert.equal(runTone("RESOLVED"), "ok");
  assert.equal(runTone("NOTHING_TO_FIX"), "idle");
  assert.equal(runTone("CHECKING"), "running");
  assert.equal(runTone("APPLYING"), "running");
  assert.equal(runTone("WAITING_APPROVAL"), "warn");
  assert.equal(runTone("HANDED_OFF"), "warn");
  for (const state of [
    "UNRESOLVED",
    "PROBE_FAILED",
    "CAS_CONFLICT",
    "RESTART_FAILED",
    "ERROR",
  ])
    assert.equal(runTone(state), "bad", state);
  assert.equal(runTone("SOMETHING_NEW"), "idle");
});

test("every run state has a Korean label and unknown states pass through", () => {
  for (const [state, label] of Object.entries(RUN_STATE_LABELS))
    assert.ok(label.length > 0 && label !== state, state);
  assert.equal(runStateLabel("WAITING_APPROVAL"), "승인 대기");
  assert.equal(runStateLabel("SOMETHING_NEW"), "SOMETHING_NEW");
});

test("approval mode reads as a person or automatic apply", () => {
  assert.equal(approvalModeLabel(false), "사람 승인");
  assert.equal(approvalModeLabel(true), "자동 적용");
});

test("change summary shows key arrows and folds the rest", () => {
  assert.equal(changeText([]), "-");
  assert.equal(
    changeText([{ key: "DB_HOST", value: "db.example.internal" }]),
    "DB_HOST → db.example.internal",
  );
  assert.equal(
    changeText([
      { key: "A", value: "1" },
      { key: "B", value: "2" },
      { key: "C", value: "3" },
    ]),
    "A → 1, B → 2 외 1개",
  );
  assert.equal(reasonText([]), "-");
  assert.equal(reasonText(["APP-01", "SEC-02"]), "APP-01, SEC-02");
});

test("the default filter hides runs that had nothing to fix", () => {
  const runs = [run("NOTHING_TO_FIX"), run("WAITING_APPROVAL"), run("ERROR")];
  assert.deepEqual(
    filterRuns(runs, "acted").map((r) => r.state),
    ["WAITING_APPROVAL", "ERROR"],
  );
  assert.equal(filterRuns(runs, "all").length, 3);
});

test("change rows keep before values when the agent sends them", () => {
  assert.deepEqual(
    changeRows({
      proposal: {
        changes: [
          { key: "DB_HOST", value: "new", before: "old" },
          { key: "DB_PORT", value: 5432 },
          { value: "no key" },
          "junk",
        ],
      },
    }),
    [
      { key: "DB_HOST", before: "old", after: "new" },
      { key: "DB_PORT", before: null, after: "5432" },
    ],
  );
  assert.deepEqual(changeRows({}), []);
  assert.deepEqual(changeRows({ proposal: { changes: "x" } }), []);
});

test("gate decisions tolerate missing detail", () => {
  const denied = gateView({ outcome: "deny", reason_codes: ["APP-01"] });
  assert.deepEqual(denied, { outcome: "deny", reasonCodes: ["APP-01"] });
  assert.deepEqual(gateView({ outcome: "allow" }), {
    outcome: "allow",
    reasonCodes: [],
  });
  assert.equal(gateView(undefined), null);
  assert.equal(gateView({ reason_codes: ["X"] }), null);
});
