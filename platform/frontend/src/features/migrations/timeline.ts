import type {
  SeriesPoint,
  StageInfo,
  StageStatus,
} from "../../api/migrations.ts";

export type WriteState = "ok" | "wait" | "fail";
export type WriteRun = { from: number; to: number; state: WriteState };
export type Domain = { from: number; to: number };
export type StageBar = {
  name: string;
  title: string;
  status: StageStatus;
  from: number;
  to: number;
};

export const WRITE_STATE_LABELS: Record<WriteState, string> = {
  ok: "정상",
  wait: "쓰기 대기",
  fail: "실패",
};

const WAIT_STATES = ["PAUSED", "SWITCHING"];

export function pointState(point: SeriesPoint, previousFailed: number) {
  if (point.failed_total > previousFailed) return "fail" as const;
  if (WAIT_STATES.includes(point.worker_state.toUpperCase()))
    return "wait" as const;
  return "ok" as const;
}

export function pointDomain(points: SeriesPoint[]): Domain | null {
  if (points.length < 2) return null;
  const times = points.map((p) => Date.parse(p.at));
  const tail = times[times.length - 1] - times[times.length - 2];
  return { from: times[0], to: times[times.length - 1] + tail };
}

export function writeRuns(points: SeriesPoint[]): WriteRun[] {
  const domain = pointDomain(points);
  if (!domain) return [];
  const runs: WriteRun[] = [];
  let previousFailed = points[0].failed_total;
  points.forEach((point, i) => {
    const from = Date.parse(point.at);
    const to = i + 1 < points.length ? Date.parse(points[i + 1].at) : domain.to;
    const state = pointState(point, previousFailed);
    previousFailed = point.failed_total;
    const last = runs[runs.length - 1];
    if (last && last.state === state) last.to = to;
    else runs.push({ from, to, state });
  });
  return runs;
}

const PADDING_MS = 2 * 60 * 1000;
const MAX_MINUTES = 1440;

const FINAL_STAGES = new Set(["COMPLETED", "ABORTED", "ROLLED_BACK"]);

export function lifetimeSpan(stages: StageInfo[]) {
  const starts = stages.flatMap((s) =>
    s.started_at === null ? [] : [Date.parse(s.started_at)],
  );
  if (starts.length === 0) return null;
  const done = stages.find(
    (s) => FINAL_STAGES.has(s.name.toUpperCase()) && s.started_at !== null,
  );
  const finished = done?.finished_at ?? done?.started_at ?? null;
  return {
    from: Math.min(...starts) - PADDING_MS,
    to: finished === null ? null : Date.parse(finished) + PADDING_MS,
  };
}

export function lifetimeWindow(allPoints: SeriesPoint[], stages: StageInfo[]) {
  const span = lifetimeSpan(stages);
  const full = pointDomain(allPoints);
  if (!span || !full) return null;
  const domain = {
    from: Math.max(span.from, full.from),
    to: Math.min(span.to ?? full.to, full.to),
  };
  const points = allPoints.filter((p) => {
    const t = Date.parse(p.at);
    return t >= domain.from && t < domain.to;
  });
  return points.length < 2 ? null : { domain, points };
}

export function lifetimeMinutes(from: number, now: number) {
  const minutes = Math.ceil((now - from) / 60000) + 5;
  return Math.min(MAX_MINUTES, Math.max(15, Math.ceil(minutes / 5) * 5));
}

export function stageBars(stages: StageInfo[], domain: Domain): StageBar[] {
  return stages.flatMap((stage) => {
    if (stage.started_at === null) return [];
    if (stage.name.toUpperCase() === "COMPLETED") return [];
    const start = Date.parse(stage.started_at);
    const end =
      stage.finished_at === null ? domain.to : Date.parse(stage.finished_at);
    if (Number.isNaN(start) || Number.isNaN(end)) return [];
    const from = Math.max(start, domain.from);
    const to = Math.min(end, domain.to);
    if (to <= from) return [];
    return [
      { name: stage.name, title: stage.title, status: stage.status, from, to },
    ];
  });
}

export function stageAt(bars: StageBar[], time: number) {
  return bars.find((bar) => time >= bar.from && time < bar.to) ?? null;
}

export function firstLastWrite(
  points: SeriesPoint[],
  key: "committed_source" | "committed_target",
) {
  let first: number | null = null;
  let last: number | null = null;
  for (let i = 1; i < points.length; i += 1) {
    if (points[i][key] > points[i - 1][key]) {
      const at = Date.parse(points[i].at);
      if (first === null) first = at;
      last = at;
    }
  }
  return { first, last };
}
