import { formatClock } from "../../lib/format";
import { buildLanes, type LaneKind } from "./lanes";
import type { StageInfo } from "../../api/migrations";

const KIND_LABELS: Record<LaneKind, string> = {
  done: "진행한 단계",
  current: "지금 단계",
  pause: "쓰기 중단 구간",
  failed: "실패",
};

export function LaneChart({
  jobs,
  now,
}: {
  jobs: { jobId: string; stages: StageInfo[] }[];
  now: number;
}) {
  const model = buildLanes(jobs, now);
  if (!model) return <p className="pk-empty">단계 기록이 아직 없어요.</p>;
  const { domain, lanes, ticks } = model;
  const span = domain.to - domain.from;
  const pct = (t: number) => `${((t - domain.from) / span) * 100}%`;
  return (
    <div className="pk-lanes">
      <div className="pk-chart-title">
        <h3>작업별 진행</h3>
      </div>
      <div className="pk-lane-axis">
        <span />
        <div>
          {ticks.map((t) => (
            <span key={t} style={{ left: pct(t) }}>
              {formatClock(t, false)}
            </span>
          ))}
        </div>
      </div>
      {lanes.map((lane) => (
        <div key={lane.jobId} className="pk-lane">
          <span className="pk-lane-name">{lane.jobId}</span>
          <div className="pk-lane-track">
            {lane.bars.map((bar) => (
              <i
                key={bar.name}
                className={`pk-bar kind-${bar.kind}`}
                title={`${bar.title}, ${formatClock(bar.from)}–${formatClock(bar.to)}`}
                style={{
                  left: pct(bar.from),
                  width: `${((bar.to - bar.from) / span) * 100}%`,
                }}
              />
            ))}
          </div>
        </div>
      ))}
      <div className="pk-legend">
        {(Object.keys(KIND_LABELS) as LaneKind[]).map((kind) => (
          <span key={kind}>
            <i className={`pk-swatch kind-${kind}`} />
            {KIND_LABELS[kind]}
          </span>
        ))}
      </div>
    </div>
  );
}
