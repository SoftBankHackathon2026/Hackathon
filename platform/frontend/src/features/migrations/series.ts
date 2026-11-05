import type { SeriesPoint } from "../../api/migrations.ts";
import { isPauseStage, stageKey } from "./stages.ts";

export type StageSpan = { stage: string; from: number; to: number };
export type PauseWindow = { from: number; to: number };

export function pointTimes(points: SeriesPoint[]) {
  return points.map((p) => Date.parse(p.at));
}

export function stageSpans(points: SeriesPoint[]): StageSpan[] {
  const times = pointTimes(points);
  const spans: StageSpan[] = [];
  points.forEach((p, i) => {
    const key = stageKey(p.stage);
    const last = spans[spans.length - 1];
    if (last && last.stage === key) {
      last.to = times[i];
      return;
    }
    if (last) last.to = times[i];
    spans.push({ stage: key, from: times[i], to: times[i] });
  });
  return spans;
}

export function pauseWindow(points: SeriesPoint[]): PauseWindow | null {
  const times = pointTimes(points);
  const first = points.findIndex((p) => isPauseStage(p.stage));
  if (first < 0) return null;
  let last = first;
  points.forEach((p, i) => {
    if (isPauseStage(p.stage)) last = i;
  });
  const end = last + 1 < points.length ? times[last + 1] : times[last];
  return { from: times[first], to: end };
}

export function firstStageTime(points: SeriesPoint[], stage: string) {
  const key = stageKey(stage);
  const found = points.find((p) => stageKey(p.stage) === key);
  return found ? Date.parse(found.at) : null;
}

export function peak(values: number[]) {
  return values.reduce((max, v) => (v > max ? v : max), 0);
}
