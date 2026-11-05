import { useId, useState } from "react";
import { formatClock, formatDateTime } from "../../lib/format";
import {
  bucketPosition,
  compactCount,
  linePath,
  niceAxis,
  spreadIndexes,
  stackValues,
  type Bucket,
} from "./chartMath";
import { useElementWidth } from "./useElementWidth";

export type GraphSeries = {
  key: string;
  label: string;
  color: string;
  kind: "bar" | "line";
  values: (number | null)[];
};
export type GraphMarker = { at: number; label: string };
export type GraphBand = { from: number; to: number };

const HEIGHT = 224;
const MARGIN = { left: 56, right: 16, top: 26, bottom: 28 };
const TOOLTIP_WIDTH = 200;

export function GraphPanel({
  title,
  summary,
  buckets,
  series,
  format,
  tickFormat = compactCount,
  axisFloor = 4,
  markers = [],
  band = null,
}: {
  title: string;
  summary: string;
  buckets: Bucket[];
  series: GraphSeries[];
  format: (value: number) => string;
  tickFormat?: (value: number) => string;
  axisFloor?: number;
  markers?: GraphMarker[];
  band?: GraphBand | null;
}) {
  const { ref, width } = useElementWidth();
  const patternId = `hatch-${useId().replace(/:/g, "")}`;
  const [hidden, setHidden] = useState<string[]>([]);
  const [hover, setHover] = useState<number | null>(null);
  const n = buckets.length;
  const inWindow = markers.filter(
    (m) => n > 0 && m.at >= buckets[0].start && m.at < buckets[n - 1].end,
  );
  const visible = series.filter((s) => !hidden.includes(s.key));
  const bars = visible.filter((s) => s.kind === "bar");
  const lines = visible.filter((s) => s.kind === "line");
  const stack = stackValues(bars.map((s) => s.values.map((v) => v ?? 0)));
  const barMax = Math.max(0, ...(stack[stack.length - 1]?.top ?? []));
  const lineMax = Math.max(
    0,
    ...lines.flatMap((s) => s.values.filter((v): v is number => v !== null)),
  );
  const axis = niceAxis(Math.max(barMax, lineMax), axisFloor);
  const plotW = Math.max(40, width - MARGIN.left - MARGIN.right);
  const plotH = HEIGHT - MARGIN.top - MARGIN.bottom;
  const slot = plotW / Math.max(n, 1);
  const y = (value: number) => MARGIN.top + plotH - (value / axis.top) * plotH;
  const x = (position: number) => MARGIN.left + slot * position;
  const gap = Math.min(2, slot * 0.2);
  const toggle = (key: string) =>
    setHidden((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    );
  const active = hover !== null && hover < n ? hover : null;
  const hoverX = active === null ? 0 : x(active + 0.5);
  const flip = hoverX > width * 0.55;

  return (
    <section className="graph-panel" aria-label={title}>
      <header className="graph-head">
        <h3>{title}</h3>
        <span className="graph-summary" title={summary}>
          {summary}
        </span>
      </header>
      <div className="chart" ref={ref}>
        {n < 2 ? (
          <p className="chart-empty" role="status">
            표시할 데이터가 아직 부족해요.
          </p>
        ) : (
          <svg
            width={width}
            height={HEIGHT}
            role="img"
            aria-label={`${title}. ${summary}`}
            className="chart-svg"
          >
            <defs>
              <pattern
                id={patternId}
                width="6"
                height="6"
                patternUnits="userSpaceOnUse"
                patternTransform="rotate(45)"
              >
                <line x1="0" y1="0" x2="0" y2="6" className="chart-hatch" />
              </pattern>
            </defs>
            {axis.ticks.map((tick) => (
              <g key={tick}>
                <line
                  x1={MARGIN.left}
                  x2={MARGIN.left + plotW}
                  y1={y(tick)}
                  y2={y(tick)}
                  className={tick === 0 ? "chart-base" : "chart-grid"}
                />
                <text
                  x={MARGIN.left - 8}
                  y={y(tick) + 4}
                  textAnchor="end"
                  className="chart-axis"
                >
                  {tickFormat(tick)}
                </text>
              </g>
            ))}
            {band && (
              <rect
                x={x(bucketPosition(buckets, band.from))}
                y={MARGIN.top}
                width={Math.max(
                  1,
                  x(bucketPosition(buckets, band.to)) -
                    x(bucketPosition(buckets, band.from)),
                )}
                height={plotH}
                fill={`url(#${patternId})`}
                className="chart-band"
              />
            )}
            {active !== null && (
              <rect
                x={x(active)}
                y={MARGIN.top}
                width={slot}
                height={plotH}
                className="chart-hover"
              />
            )}
            {bars.map((s, j) =>
              s.values.map((value, i) =>
                value && value > 0 ? (
                  <rect
                    key={`${s.key}-${i}`}
                    x={x(i) + gap / 2}
                    y={y(stack[j].top[i])}
                    width={Math.max(slot - gap, 1)}
                    height={Math.max(
                      y(stack[j].bottom[i]) - y(stack[j].top[i]),
                      1,
                    )}
                    fill={s.color}
                  />
                ) : null,
              ),
            )}
            {lines.map((s) => (
              <path
                key={s.key}
                d={linePath(
                  buckets.map((_, i) => x(i + 0.5)),
                  s.values.map((v) => (v === null ? Number.NaN : y(v))),
                )}
                fill="none"
                stroke={s.color}
                strokeWidth={2}
                strokeLinejoin="round"
              />
            ))}
            {inWindow.map((marker, i) => {
              const mx = x(bucketPosition(buckets, marker.at));
              const next = inWindow[i + 1];
              const labelW = marker.label.length * 12 + 8;
              const crowded =
                next !== undefined &&
                x(bucketPosition(buckets, next.at)) - mx < labelW + 12;
              const nearEdge = mx > width - labelW - 24;
              const flip = crowded || nearEdge;
              const fits = !flip || mx - 8 - labelW > MARGIN.left;
              return (
                <g key={marker.label}>
                  <line
                    x1={mx}
                    x2={mx}
                    y1={MARGIN.top - 8}
                    y2={MARGIN.top + plotH}
                    className="chart-marker"
                  />
                  <rect
                    x={mx - 3}
                    y={MARGIN.top - 12}
                    width={6}
                    height={6}
                    className="chart-marker-handle"
                  />
                  {fits && (
                    <text
                      x={flip ? mx - 8 : mx + 8}
                      y={MARGIN.top - 5}
                      textAnchor={flip ? "end" : "start"}
                      className="chart-marker-label"
                    >
                      {marker.label}
                    </text>
                  )}
                </g>
              );
            })}
            {active !== null && (
              <line
                x1={hoverX}
                x2={hoverX}
                y1={MARGIN.top}
                y2={MARGIN.top + plotH}
                className="chart-cross"
              />
            )}
            {active !== null &&
              lines.map((s) => {
                const v = s.values[active];
                return v === null ? null : (
                  <circle
                    key={s.key}
                    cx={hoverX}
                    cy={y(v)}
                    r={3.5}
                    fill={s.color}
                  />
                );
              })}
            {spreadIndexes(n).map((i) => (
              <text
                key={i}
                x={x(i + 0.5)}
                y={HEIGHT - 8}
                textAnchor="middle"
                className="chart-axis"
              >
                {formatClock(buckets[i].start, false)}
              </text>
            ))}
            <rect
              x={MARGIN.left}
              y={MARGIN.top}
              width={plotW}
              height={plotH}
              fill="transparent"
              onPointerMove={(event) => {
                const box = event.currentTarget.getBoundingClientRect();
                const i = Math.floor(
                  ((event.clientX - box.left) / box.width) * n,
                );
                setHover(Math.min(Math.max(i, 0), n - 1));
              }}
              onPointerLeave={() => setHover(null)}
            />
          </svg>
        )}
        {active !== null && (
          <div
            className="chart-tip"
            style={{
              top: MARGIN.top,
              width: TOOLTIP_WIDTH,
              left: Math.max(
                0,
                flip ? hoverX - 16 - TOOLTIP_WIDTH : hoverX + 16,
              ),
            }}
          >
            <div className="chart-tip-time">
              {formatDateTime(new Date(buckets[active].start).toISOString())}
            </div>
            {visible.map((s) => {
              const v = s.values[active];
              return (
                <div className="chart-tip-row" key={s.key}>
                  <i className="swatch" style={{ background: s.color }} />
                  <span>{s.label}</span>
                  <b>{v === null ? "-" : format(v)}</b>
                </div>
              );
            })}
          </div>
        )}
      </div>
      <div className="graph-legend">
        {series.map((s) => (
          <button
            key={s.key}
            type="button"
            aria-pressed={!hidden.includes(s.key)}
            className={
              hidden.includes(s.key) ? "legend-item off" : "legend-item"
            }
            onClick={() => toggle(s.key)}
          >
            <i className="swatch" style={{ background: s.color }} />
            {s.label}
          </button>
        ))}
      </div>
    </section>
  );
}
