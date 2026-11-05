import type { SeriesPoint, StageInfo } from "../../api/migrations.ts";
import { pointTimes } from "./series.ts";
import { stageKey } from "./stages.ts";

export type TrafficBucket = {
  start: number;
  end: number;
  source: number;
  target: number;
  queue: number;
  p95: number | null;
  max: number | null;
  failed: number;
};

function maxOrNull(values: (number | null)[]) {
  const present = values.filter((v): v is number => v !== null);
  return present.length === 0 ? null : Math.max(...present);
}

export function bucketTraffic(
  points: SeriesPoint[],
  count = 60,
): TrafficBucket[] {
  const n = points.length;
  if (n < 2) return [];
  const size = Math.min(count, n);
  const times = pointTimes(points);
  const tail = times[n - 1] - times[n - 2];
  let prevSource = points[0].committed_source;
  let prevTarget = points[0].committed_target;
  let prevFailed = points[0].failed_total;
  return Array.from({ length: size }, (_, i) => {
    const from = Math.floor((i * n) / size);
    const to = Math.floor(((i + 1) * n) / size);
    const slice = points.slice(from, to);
    const last = slice[slice.length - 1];
    const bucket: TrafficBucket = {
      start: times[from],
      end: to < n ? times[to] : times[n - 1] + tail,
      source: Math.max(0, last.committed_source - prevSource),
      target: Math.max(0, last.committed_target - prevTarget),
      queue: Math.max(...slice.map((p) => p.queue_length)),
      p95: maxOrNull(slice.map((p) => p.latency_p95)),
      max: maxOrNull(slice.map((p) => p.latency_max)),
      failed: Math.max(0, last.failed_total - prevFailed),
    };
    prevSource = last.committed_source;
    prevTarget = last.committed_target;
    prevFailed = last.failed_total;
    return bucket;
  });
}

export function stageMarkers(stages: StageInfo[]) {
  const wanted = [
    { name: "READY", label: "승인" },
    { name: "SWITCHING", label: "전환" },
  ];
  return wanted.flatMap(({ name, label }) => {
    const stage = stages.find((s) => stageKey(s.name) === name);
    const at = stage?.started_at ? Date.parse(stage.started_at) : Number.NaN;
    return Number.isNaN(at) ? [] : [{ at, label }];
  });
}
