import type { StageInfo } from "../../api/migrations.ts";
import { isPauseStage } from "./stages.ts";
import { lifetimeSpan } from "./timeline.ts";

export type LaneKind = "done" | "current" | "pause" | "failed";
export type LaneBar = {
  name: string;
  title: string;
  from: number;
  to: number;
  kind: LaneKind;
};
export type Lane = { jobId: string; bars: LaneBar[] };

const PAD_MS = 2 * 60 * 1000;

export function buildLanes(
  jobs: { jobId: string; stages: StageInfo[] }[],
  now: number,
) {
  const lanes: Lane[] = jobs.map((job) => ({
    jobId: job.jobId,
    bars: job.stages.flatMap((stage): LaneBar[] => {
      if (stage.started_at === null) return [];
      if (stage.name.toUpperCase() === "COMPLETED") return [];
      const from = Date.parse(stage.started_at);
      if (stage.finished_at === null && stage.status !== "current") return [];
      const to =
        stage.finished_at === null ? now : Date.parse(stage.finished_at);
      if (Number.isNaN(from) || Number.isNaN(to) || to <= from) return [];
      const kind: LaneKind =
        stage.status === "failed"
          ? "failed"
          : isPauseStage(stage.name)
            ? "pause"
            : stage.status === "current"
              ? "current"
              : "done";
      return [{ name: stage.name, title: stage.title, from, to, kind }];
    }),
  }));
  const spans = jobs.flatMap((job) => {
    const span = lifetimeSpan(job.stages);
    return span ? [span] : [];
  });
  if (spans.length === 0) return null;
  const from = Math.min(...spans.map((s) => s.from));
  const to = Math.max(...spans.map((s) => s.to ?? now + PAD_MS));
  const ticks = Array.from(
    { length: 6 },
    (_, i) => from + ((to - from) * i) / 5,
  );
  return { domain: { from, to }, lanes, ticks };
}
