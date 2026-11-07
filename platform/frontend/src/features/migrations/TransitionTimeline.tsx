import { useState } from "react";
import type { SeriesPoint, StageInfo } from "../../api/migrations";
import { clampIndex, nearestIndex } from "../../components/charts/chartMath";
import { useElementWidth } from "../../components/charts/useElementWidth";
import { formatClock, formatDateTime } from "../../lib/format";
import { stageNotes } from "./stages";
import {
  lifetimeWindow,
  pointState,
  stageAt,
  stageBars,
  WRITE_STATE_LABELS,
  writeRuns,
  type WriteState,
} from "./timeline";

const LABEL_W = 80;
const RIGHT_PAD = 28;
const ROW_H = 28;
const MARKER_Y = 10;
const AXIS_Y = 40;
const STAGE_Y = 48;
const WRITE_Y = STAGE_Y + ROW_H + 8;
const HEIGHT = WRITE_Y + ROW_H + 4;
const TOOLTIP_W = 200;

const LEGEND: WriteState[] = ["ok", "wait", "fail"];

export function TransitionTimeline({
  allPoints,
  stages,
}: {
  allPoints: SeriesPoint[];
  stages: StageInfo[];
}) {
  const { ref, width } = useElementWidth();
  const [pointer, setHover] = useState<number | null>(null);
  const window = lifetimeWindow(allPoints, stages);
  if (!window)
    return (
      <p className="chart-empty" role="status">
        전환을 시작하면 단계별 시간이 여기에 표시돼요.
      </p>
    );
  const { domain, points } = window;
  const hover = clampIndex(pointer, points.length);
  const plotW = Math.max(120, width - LABEL_W - RIGHT_PAD);
  const width_ms = domain.to - domain.from;
  const x = (t: number) => LABEL_W + ((t - domain.from) / width_ms) * plotW;
  const bars = stageBars(stages, domain);
  const runs = writeRuns(points);
  const times = points.map((p) => Date.parse(p.at));
  const markers = stages
    .flatMap((stage) => {
      if (stage.started_at === null) return [];
      const at = Date.parse(stage.started_at);
      const notes = stageNotes(stage);
      return (
        stage.name.toUpperCase() === "COMPLETED" ? [...notes, "완료"] : notes
      ).map((label) => ({ at, label }));
    })
    .filter((m) => m.at >= domain.from && m.at < domain.to)
    .sort((a, b) => a.at - b.at);
  const ticks = Array.from(
    { length: 7 },
    (_, i) => domain.from + (width_ms * i) / 6,
  );
  const active = hover === null ? null : points[hover];
  const activeX = hover === null ? 0 : x(times[hover]);
  const activeStage = hover === null ? null : stageAt(bars, times[hover]);
  const activeState =
    hover === null
      ? "ok"
      : pointState(
          points[hover],
          hover > 0 ? points[hover - 1].failed_total : points[0].failed_total,
        );
  let doneIndex = 0;

  return (
    <div className="chart tl" ref={ref}>
      <svg
        width={width}
        height={HEIGHT}
        role="img"
        aria-label="단계와 쓰기 상태 타임라인"
        className="chart-svg"
      >
        {ticks.map((t) => (
          <g key={t}>
            <line
              x1={x(t)}
              x2={x(t)}
              y1={AXIS_Y + 2}
              y2={STAGE_Y}
              className="chart-base"
            />
            <text
              x={x(t)}
              y={AXIS_Y - 4}
              textAnchor="middle"
              className="chart-axis"
            >
              {formatClock(t, false)}
            </text>
          </g>
        ))}
        <text x={0} y={STAGE_Y + ROW_H / 2 + 5} className="tl-row-label">
          단계
        </text>
        <text x={0} y={WRITE_Y + ROW_H / 2 + 5} className="tl-row-label">
          쓰기 상태
        </text>
        <rect
          x={LABEL_W}
          y={STAGE_Y}
          width={plotW}
          height={ROW_H}
          className="tl-empty"
        />
        <rect
          x={LABEL_W}
          y={WRITE_Y}
          width={plotW}
          height={ROW_H}
          className="tl-empty"
        />
        {bars.map((bar) => {
          const left = x(bar.from);
          const w = x(bar.to) - left;
          const title = bar.title;
          const kind =
            bar.status === "failed"
              ? "tl-failed"
              : bar.status === "current"
                ? "tl-current"
                : doneIndex++ % 2 === 0
                  ? "tl-done-a"
                  : "tl-done-b";
          return (
            <g key={bar.name}>
              <rect
                x={left}
                y={STAGE_Y}
                width={Math.max(w, 1)}
                height={ROW_H}
                className={`tl-seg ${kind}`}
              >
                <title>{title}</title>
              </rect>
              {w > title.length * 12 + 12 && (
                <text
                  x={left + w / 2}
                  y={STAGE_Y + ROW_H / 2 + 5}
                  textAnchor="middle"
                  className={`tl-seg-label ${kind}`}
                >
                  {title}
                </text>
              )}
            </g>
          );
        })}
        {runs.map((run) => (
          <rect
            key={`${run.from}-${run.state}`}
            x={x(run.from)}
            y={WRITE_Y}
            width={Math.max(x(run.to) - x(run.from), 1)}
            height={ROW_H}
            className={`tl-write tl-${run.state}`}
          >
            <title>{WRITE_STATE_LABELS[run.state]}</title>
          </rect>
        ))}
        {markers.map((marker, i) => {
          const mx = x(marker.at);
          const next = markers[i + 1];
          const flip =
            (next !== undefined && x(next.at) - mx < 150) || mx > width - 140;
          return (
            <g key={`${marker.at}-${marker.label}`}>
              <line
                x1={mx}
                x2={mx}
                y1={MARKER_Y}
                y2={WRITE_Y + ROW_H}
                className="chart-marker"
              />
              <rect
                x={mx - 3}
                y={MARKER_Y - 3}
                width={6}
                height={6}
                className="chart-marker-handle"
              />
              <text
                x={flip ? mx - 8 : mx + 8}
                y={MARKER_Y + 4}
                textAnchor={flip ? "end" : "start"}
                className="chart-marker-label"
              >
                {marker.label}
              </text>
            </g>
          );
        })}
        {active && (
          <line
            x1={activeX}
            x2={activeX}
            y1={AXIS_Y + 2}
            y2={WRITE_Y + ROW_H}
            className="chart-cross"
          />
        )}
        <rect
          x={LABEL_W}
          y={STAGE_Y}
          width={plotW}
          height={WRITE_Y + ROW_H - STAGE_Y}
          fill="transparent"
          onPointerMove={(event) => {
            const box = event.currentTarget.getBoundingClientRect();
            const ratio = (event.clientX - box.left) / box.width;
            setHover(nearestIndex(times, domain.from + ratio * width_ms));
          }}
          onPointerLeave={() => setHover(null)}
        />
      </svg>
      {active && (
        <div
          className="chart-tip"
          style={{
            top: STAGE_Y,
            width: TOOLTIP_W,
            left: Math.max(
              0,
              activeX > width * 0.6 ? activeX - 16 - TOOLTIP_W : activeX + 16,
            ),
          }}
        >
          <div className="chart-tip-time">{formatDateTime(active.at)}</div>
          <div className="chart-tip-row">
            <span>단계</span>
            <b>{activeStage ? activeStage.title : "-"}</b>
          </div>
          <div className="chart-tip-row">
            <span>쓰기 상태</span>
            <b>{WRITE_STATE_LABELS[activeState]}</b>
          </div>
          <div className="chart-tip-row">
            <span>워커</span>
            <b>{active.worker_state}</b>
          </div>
          <div className="chart-tip-row">
            <span>대기열</span>
            <b>{active.queue_length.toLocaleString("ko-KR")}건</b>
          </div>
        </div>
      )}
      <div className="graph-legend tl-legend">
        {LEGEND.map((state) => (
          <span key={state} className="legend-static">
            <i className={`swatch tl-${state}`} />
            {WRITE_STATE_LABELS[state]}
          </span>
        ))}
      </div>
    </div>
  );
}
