import assert from "node:assert/strict";
import { test } from "node:test";
import type { AgentCommand } from "../src/api/agent.ts";
import {
  changeLines,
  commandText,
  draftError,
  draftFrom,
  hasPendingRerun,
  onOffLabel,
  readConfig,
  settingsChange,
} from "../src/features/agent/settings.ts";

const CONFIG = {
  auto_apply: false,
  interval_seconds: 60,
  job_id: "forward_001",
  stage: "pre_cutover",
  slack: { mode: "bot", enabled: true, channel: "C000TEST", thread_ts: null },
  fix: {
    keys: ["DB_HOST", "DB_PORT"],
    plan: { DB_HOST: "db.example.internal" },
  },
  sandbox: { runner: "opensandbox", image: "codex:test", model: null },
  settings_applied_at: "2026-11-07T00:00:00Z",
};

function command(
  kind: AgentCommand["kind"],
  payload: Record<string, unknown> = {},
): AgentCommand {
  return {
    id: 1,
    kind,
    payload,
    requested_by: "운영 화면",
    requested_at: "2026-11-07T00:00:00Z",
  };
}

test("config view reads the agent's reported settings", () => {
  const view = readConfig(CONFIG);
  assert.ok(view);
  assert.equal(view.autoApply, false);
  assert.equal(view.intervalSeconds, 60);
  assert.deepEqual(view.slack, {
    mode: "봇",
    enabled: true,
    channel: "C000TEST",
    threadTs: null,
  });
  assert.deepEqual(view.fixKeys, ["DB_HOST", "DB_PORT"]);
  assert.deepEqual(view.plan, [
    { key: "DB_HOST", value: "db.example.internal" },
  ]);
  assert.deepEqual(view.sandbox, {
    runner: "opensandbox",
    image: "codex:test",
    model: null,
  });
  assert.equal(view.appliedAt, "2026-11-07T00:00:00Z");
  assert.equal(readConfig({ slack: { mode: "webhook" } })?.slack.mode, "웹훅");
  assert.equal(readConfig({})?.slack.mode, "없음");
  assert.equal(readConfig(null), null);
});

test("config view tolerates missing or odd fields", () => {
  const view = readConfig({ fix: { keys: "x", plan: [] }, auto_apply: "yes" });
  assert.ok(view);
  assert.equal(view.autoApply, null);
  assert.equal(view.intervalSeconds, null);
  assert.deepEqual(view.fixKeys, []);
  assert.deepEqual(view.plan, []);
  assert.equal(onOffLabel(null), "-");
  assert.equal(onOffLabel(true), "켜짐");
  assert.equal(onOffLabel(false), "꺼짐");
});

test("only changed settings are sent", () => {
  const base = draftFrom(readConfig(CONFIG)!);
  assert.deepEqual(base, {
    autoApply: false,
    interval: "60",
    slackEnabled: true,
    threadTs: "",
  });
  assert.deepEqual(settingsChange(base, base), {});
  assert.deepEqual(settingsChange(base, { ...base, interval: " 60 " }), {});
  assert.deepEqual(
    settingsChange(base, {
      autoApply: true,
      interval: "120",
      slackEnabled: false,
      threadTs: " 1730000000.123456 ",
    }),
    {
      auto_apply: true,
      interval_seconds: 120,
      slack_enabled: false,
      slack_thread_ts: "1730000000.123456",
    },
  );
  const threaded = { ...base, threadTs: "1730000000.1" };
  assert.deepEqual(settingsChange(threaded, { ...threaded, threadTs: "" }), {
    slack_thread_ts: "",
  });
});

test("draft validation matches the server limits", () => {
  const base = draftFrom(readConfig(CONFIG)!);
  assert.equal(draftError(base), null);
  assert.equal(draftError({ ...base, interval: "30" }), null);
  assert.equal(draftError({ ...base, interval: "3600" }), null);
  for (const interval of ["29", "3601", "", "60.5", "abc"])
    assert.ok(draftError({ ...base, interval }), interval);
  assert.equal(draftError({ ...base, threadTs: "1730000000.123456789" }), null);
  for (const threadTs of ["12345.1", "1730000000.1234567890", "abc"])
    assert.ok(draftError({ ...base, threadTs }), threadTs);
});

test("commands read as short Korean lines", () => {
  assert.equal(commandText(command("rerun")), "지금 다시 판정");
  assert.equal(
    commandText(
      command("settings", { auto_apply: true, interval_seconds: 120 }),
    ),
    "설정 변경: 승인 모드 자동 적용, 판정 주기 120초",
  );
  assert.deepEqual(changeLines({ slack_enabled: false, slack_thread_ts: "" }), [
    "Slack 알림 꺼짐",
    "스레드 없이 채널에 바로",
  ]);
  assert.deepEqual(changeLines({ slack_thread_ts: "1730000000.1" }), [
    "스레드 ts 1730000000.1",
  ]);
  assert.equal(hasPendingRerun([command("settings")]), false);
  assert.equal(hasPendingRerun([command("settings"), command("rerun")]), true);
});
