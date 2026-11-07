import type { AgentCommand, AgentSettingsChange } from "../../api/agent.ts";
import { approvalModeLabel } from "./model.ts";

export const INTERVAL_MIN = 30;
export const INTERVAL_MAX = 3600;
const THREAD_TS = /^\d{6,}\.\d{1,9}$/;

const SLACK_MODE_LABELS: Record<string, string> = {
  webhook: "웹훅",
  bot: "봇",
  none: "없음",
};

export type ConfigView = {
  autoApply: boolean | null;
  intervalSeconds: number | null;
  jobId: string | null;
  stage: string | null;
  slack: {
    mode: string;
    enabled: boolean | null;
    channel: string | null;
    threadTs: string | null;
  };
  fixKeys: string[];
  plan: { key: string; value: string }[];
  sandbox: {
    runner: string | null;
    image: string | null;
    model: string | null;
  };
  appliedAt: string | null;
};

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown) {
  return typeof value === "string" && value !== "" ? value : null;
}

function flag(value: unknown) {
  return typeof value === "boolean" ? value : null;
}

export function readConfig(
  raw: Record<string, unknown> | null,
): ConfigView | null {
  if (raw === null) return null;
  const slack = record(raw.slack);
  const fix = record(raw.fix);
  const sandbox = record(raw.sandbox);
  const mode = text(slack.mode) ?? "none";
  return {
    autoApply: flag(raw.auto_apply),
    intervalSeconds:
      typeof raw.interval_seconds === "number" ? raw.interval_seconds : null,
    jobId: text(raw.job_id),
    stage: text(raw.stage),
    slack: {
      mode: SLACK_MODE_LABELS[mode] ?? mode,
      enabled: flag(slack.enabled),
      channel: text(slack.channel),
      threadTs: text(slack.thread_ts),
    },
    fixKeys: Array.isArray(fix.keys) ? fix.keys.map(String) : [],
    plan: Object.entries(record(fix.plan)).map(([key, value]) => ({
      key,
      value: String(value),
    })),
    sandbox: {
      runner: text(sandbox.runner),
      image: text(sandbox.image),
      model: text(sandbox.model),
    },
    appliedAt: text(raw.settings_applied_at),
  };
}

export function onOffLabel(value: boolean | null) {
  if (value === null) return "-";
  return value ? "켜짐" : "꺼짐";
}

export type SettingsDraft = {
  autoApply: boolean;
  interval: string;
  slackEnabled: boolean;
  threadTs: string;
};

export function draftFrom(config: ConfigView): SettingsDraft {
  return {
    autoApply: config.autoApply ?? false,
    interval:
      config.intervalSeconds === null ? "" : String(config.intervalSeconds),
    slackEnabled: config.slack.enabled ?? false,
    threadTs: config.slack.threadTs ?? "",
  };
}

export function draftError(draft: SettingsDraft) {
  const interval = Number(draft.interval.trim());
  if (
    !/^\d+$/.test(draft.interval.trim()) ||
    interval < INTERVAL_MIN ||
    interval > INTERVAL_MAX
  )
    return `판정 주기는 ${INTERVAL_MIN}~${INTERVAL_MAX}초 사이 정수로 넣어 주세요.`;
  const ts = draft.threadTs.trim();
  if (ts !== "" && !THREAD_TS.test(ts))
    return "스레드 ts 는 1700000000.123456 같은 형식이에요. 비우면 채널에 바로 보내요.";
  return null;
}

export function settingsChange(
  base: SettingsDraft,
  draft: SettingsDraft,
): AgentSettingsChange {
  const change: AgentSettingsChange = {};
  if (draft.autoApply !== base.autoApply) change.auto_apply = draft.autoApply;
  if (draft.interval.trim() !== base.interval.trim())
    change.interval_seconds = Number(draft.interval.trim());
  if (draft.slackEnabled !== base.slackEnabled)
    change.slack_enabled = draft.slackEnabled;
  if (draft.threadTs.trim() !== base.threadTs.trim())
    change.slack_thread_ts = draft.threadTs.trim();
  return change;
}

export function changeLines(change: AgentSettingsChange) {
  const lines: string[] = [];
  if (change.auto_apply !== undefined)
    lines.push(`승인 모드 ${approvalModeLabel(change.auto_apply)}`);
  if (change.interval_seconds !== undefined)
    lines.push(`판정 주기 ${change.interval_seconds}초`);
  if (change.slack_enabled !== undefined)
    lines.push(`Slack 알림 ${onOffLabel(change.slack_enabled)}`);
  if (change.slack_thread_ts !== undefined)
    lines.push(
      change.slack_thread_ts === ""
        ? "스레드 없이 채널에 바로"
        : `스레드 ts ${change.slack_thread_ts}`,
    );
  return lines;
}

export function commandText(command: AgentCommand) {
  if (command.kind === "rerun") return "지금 다시 판정";
  const lines = changeLines(command.payload as AgentSettingsChange);
  return `설정 변경: ${lines.length > 0 ? lines.join(", ") : "-"}`;
}

export function hasPendingRerun(commands: AgentCommand[]) {
  return commands.some((command) => command.kind === "rerun");
}
