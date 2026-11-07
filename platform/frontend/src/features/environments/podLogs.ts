import type { PodLogLine } from "../../api/migrations.ts";

export const TAIL_OPTIONS = [200, 500, 1000] as const;
export const FOLLOW_MS = 5000;

export type LogLevel = "error" | "warn" | null;

const ERROR =
  /\b\w*(error|exception)\b|\b(erro|fatal|panic|traceback|critical|failed|refused)\b/i;
const WARN = /\b(warn|warning|deprecated)\b/i;
const KLOG = /^([IWEF])\d{4}\s/;
const LEVEL_FIELD = /\blevel[=:]\s*"?([a-z]+)/i;
const LEVEL_WORD =
  /(?:^|[\s[|(])(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERRO|FATAL|FATA|CRITICAL|PANIC)(?=[\s\]|):]|$)/;
const LEVELS: Record<string, LogLevel> = {
  trace: null,
  debug: null,
  info: null,
  notice: null,
  i: null,
  warn: "warn",
  warning: "warn",
  w: "warn",
  error: "error",
  erro: "error",
  fatal: "error",
  fata: "error",
  critical: "error",
  panic: "error",
  e: "error",
  f: "error",
};

function declaredLevel(message: string): LogLevel | undefined {
  const head = message.slice(0, 80);
  const found =
    KLOG.exec(head)?.[1] ??
    LEVEL_FIELD.exec(head)?.[1] ??
    LEVEL_WORD.exec(head)?.[1];
  return found === undefined ? undefined : LEVELS[found.toLowerCase()];
}

export function logLevel(message: string): LogLevel {
  const declared = declaredLevel(message);
  if (declared !== undefined) return declared;
  if (ERROR.test(message)) return "error";
  if (WARN.test(message)) return "warn";
  return null;
}

export function matches(message: string, query: string) {
  const needle = query.trim().toLowerCase();
  return message.toLowerCase().includes(needle);
}

export function highlight(message: string, query: string) {
  const needle = query.trim().toLowerCase();
  if (!needle) return [{ text: message, hit: false }];
  const parts: { text: string; hit: boolean }[] = [];
  const lower = message.toLowerCase();
  let from = 0;
  for (
    let at = lower.indexOf(needle);
    at !== -1;
    at = lower.indexOf(needle, from)
  ) {
    if (at > from) parts.push({ text: message.slice(from, at), hit: false });
    parts.push({ text: message.slice(at, at + needle.length), hit: true });
    from = at + needle.length;
  }
  if (from < message.length)
    parts.push({ text: message.slice(from), hit: false });
  return parts;
}

export function logTime(at: string | null, now: number) {
  if (!at) return "";
  const ms = Date.parse(at);
  if (Number.isNaN(ms)) return "";
  const date = new Date(ms);
  const time = date.toLocaleTimeString("ko-KR", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  if (new Date(now).toDateString() === date.toDateString()) return time;
  const day = date.toLocaleDateString("ko-KR", {
    month: "2-digit",
    day: "2-digit",
  });
  return `${day} ${time}`;
}

export function logText(lines: PodLogLine[]) {
  return lines
    .map((line) => (line.at ? `${line.at} ${line.message}` : line.message))
    .join("\n");
}

export function logFileName(pod: string, container: string, previous: boolean) {
  return `${pod}_${container}${previous ? "_previous" : ""}.log`;
}

export function levelCounts(lines: PodLogLine[]) {
  let error = 0;
  let warn = 0;
  for (const line of lines) {
    const level = logLevel(line.message);
    if (level === "error") error += 1;
    else if (level === "warn") warn += 1;
  }
  return { error, warn };
}

export function atBottom(
  el: { scrollTop: number; scrollHeight: number; clientHeight: number },
  slack = 24,
) {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= slack;
}

export function newLineCount(
  before: PodLogLine[],
  after: PodLogLine[],
): number {
  if (before.length === 0) return 0;
  const last = before[before.length - 1];
  for (let i = after.length - 1; i >= 0; i -= 1) {
    const line = after[i];
    if (line.at === last.at && line.message === last.message)
      return after.length - 1 - i;
  }
  return after.length;
}

type Scrollable = {
  scrollHeight: number;
  clientHeight: number;
  scrollTop: number;
};

export function canScroll(el: Scrollable, deltaY: number) {
  if (el.scrollHeight <= el.clientHeight) return false;
  if (deltaY < 0) return el.scrollTop > 0;
  return el.scrollTop + el.clientHeight < el.scrollHeight;
}

export function keepsWheel(target: Element, deltaY: number) {
  for (
    let el: Element | null = target;
    el && !el.classList.contains("fp-wide");
    el = el.parentElement
  ) {
    const style = getComputedStyle(el);
    if (
      /(auto|scroll)/.test(style.overflowY) &&
      canScroll(el as HTMLElement, deltaY)
    )
      return true;
  }
  return false;
}
