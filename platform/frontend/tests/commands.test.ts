import assert from "node:assert/strict";
import { test } from "node:test";
import type { CutoverCommand, StageInfo } from "../src/api/migrations.ts";
import {
  activeCommand,
  availableActions,
  commandResultText,
  commandTone,
  failedBeforeSwitch,
  visibleActions,
} from "../src/features/migrations/commands.ts";

function command(
  status: CutoverCommand["status"],
  extra: Partial<CutoverCommand> = {},
): CutoverCommand {
  return {
    id: `cmd-${status}`,
    job_id: "forward_001",
    action: "run",
    approval_id: null,
    note: null,
    requested_by: null,
    requested_at: "2026-11-07T00:00:00Z",
    status,
    started_at: null,
    finished_at: null,
    exit_code: null,
    stage: null,
    log_tail: null,
    ...extra,
  };
}

const FLOW = [
  "PRECHECK",
  "COPYING",
  "CATCHING_UP",
  "READY",
  "FENCING",
  "DRAINING",
  "SYNC_CONFIRMED",
  "SWITCHING",
  "VERIFYING",
  "COMPLETED",
];

function stages(failed: string | null): StageInfo[] {
  return FLOW.map((name) => ({
    name,
    title: name,
    description: "",
    status: name === failed ? "failed" : "pending",
    started_at: null,
    finished_at: null,
    point_of_no_return: name === "SWITCHING",
    evidence: {},
  }));
}

test("visibleActions offers run before READY and approve only at READY", () => {
  assert.deepEqual(visibleActions(null, false), ["run"]);
  assert.deepEqual(visibleActions("COPYING", false), ["run"]);
  assert.deepEqual(visibleActions("READY", false), ["approve"]);
  assert.deepEqual(visibleActions("COMPLETED", false), []);
});

test("visibleActions offers retry and abort after ABORTED, abort only before SWITCHING", () => {
  assert.deepEqual(visibleActions("ABORTED", true), ["retry", "abort"]);
  assert.deepEqual(visibleActions("ABORTED", false), ["retry"]);
  assert.deepEqual(visibleActions("NEEDS_RECOVERY", true), ["force_retry"]);
});

test("failedBeforeSwitch is true only when the failed stage precedes the point of no return", () => {
  assert.equal(failedBeforeSwitch(stages("DRAINING")), true);
  assert.equal(failedBeforeSwitch(stages("SWITCHING")), false);
  assert.equal(failedBeforeSwitch(stages("VERIFYING")), false);
  assert.equal(failedBeforeSwitch(stages(null)), false);
});

test("availableActions disables everything while a command is pending or running", () => {
  const idle = availableActions("READY", undefined);
  assert.equal(idle.approve, true);
  assert.equal(idle.run, false);
  for (const status of ["pending", "running"] as const) {
    const locked = availableActions("READY", command(status));
    assert.deepEqual(Object.values(locked), [false, false, false, false, false]);
  }
  const after = availableActions("READY", command("waiting_approval"));
  assert.equal(after.approve, false, "finished commands are not passed as active");
});

test("activeCommand finds the pending or running command regardless of order", () => {
  const list = [
    command("failed"),
    command("running", { id: "cmd-r" }),
    command("succeeded"),
  ];
  assert.equal(activeCommand(list)?.id, "cmd-r");
  assert.equal(activeCommand([command("failed")]), undefined);
  assert.equal(activeCommand(undefined), undefined);
});

test("commandTone and commandResultText summarize finished commands", () => {
  assert.equal(commandTone("waiting_approval"), "warn");
  assert.equal(commandTone("failed"), "bad");
  assert.equal(commandTone("pending"), "idle");
  assert.equal(commandResultText(command("running")), "-");
  assert.equal(
    commandResultText(
      command("waiting_approval", { exit_code: 3, stage: "READY" }),
    ),
    "exit 3, READY",
  );
  assert.equal(commandResultText(command("failed", { exit_code: 1 })), "exit 1");
});
