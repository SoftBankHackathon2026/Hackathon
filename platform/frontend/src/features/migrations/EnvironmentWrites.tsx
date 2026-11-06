import type { MigrationDetail, MigrationSeries } from "../../api/migrations";
import { BarCell } from "../../components/BarCell";
import { StatusDot } from "../../components/StatusDot";
import { GraphPanel } from "../../components/charts/GraphPanel";
import {
  formatAgo,
  formatClock,
  formatCount,
  formatRate,
  formatSeconds,
} from "../../lib/format";
import { pauseWindow, peak } from "./series";
import { firstLastWrite } from "./timeline";
import { bucketTraffic, stageMarkers } from "./traffic";

export function EnvironmentGraphs({
  series,
  detail,
  sourceName,
  targetName,
}: {
  series: MigrationSeries;
  detail: MigrationDetail;
  sourceName: string;
  targetName: string;
}) {
  const { points } = series;
  const buckets = bucketTraffic(points);
  const shared = {
    buckets,
    markers: stageMarkers(detail.stages),
    band: pauseWindow(points),
  };
  const sum = (pick: (b: (typeof buckets)[number]) => number) =>
    buckets.reduce((total, b) => total + pick(b), 0);
  const last = buckets[buckets.length - 1];
  return (
    <div className="graph-row">
      <GraphPanel
        {...shared}
        title="쓰기 건수"
        summary={`${sourceName} ${formatCount(sum((b) => b.source))}, ${targetName} ${formatCount(sum((b) => b.target))}`}
        format={formatCount}
        series={[
          {
            key: "source",
            label: `${sourceName} (원본)`,
            color: "var(--series-source)",
            kind: "bar",
            values: buckets.map((b) => b.source),
          },
          {
            key: "target",
            label: `${targetName} (대상)`,
            color: "var(--series-target)",
            kind: "bar",
            values: buckets.map((b) => b.target),
          },
        ]}
      />
      <GraphPanel
        {...shared}
        title="대기열 길이"
        summary={`최대 ${formatCount(peak(buckets.map((b) => b.queue)))}건`}
        format={(v) => `${formatCount(v)}건`}
        series={[
          {
            key: "queue",
            label: "대기열",
            color: "var(--series-queue)",
            kind: "bar",
            values: buckets.map((b) => b.queue),
          },
        ]}
      />
      <GraphPanel
        {...shared}
        title="응답 지연 (p95)"
        summary={`마지막 ${formatSeconds(last?.p95 ?? null)}`}
        format={formatSeconds}
        tickFormat={formatSeconds}
        axisFloor={0.1}
        series={[
          {
            key: "p95",
            label: "p95",
            color: "var(--series-source)",
            kind: "line",
            values: buckets.map((b) => b.p95),
          },
        ]}
      />
    </div>
  );
}

function WriteTime({ at }: { at: number | null }) {
  if (at === null) return <span className="sub">-</span>;
  return (
    <span>
      {formatClock(at)}{" "}
      <span className="sub">{formatAgo(new Date(at).toISOString())}</span>
    </span>
  );
}

export function EnvironmentTable({
  series,
  detail,
  sourceName,
  targetName,
}: {
  series: MigrationSeries | undefined;
  detail: MigrationDetail;
  sourceName: string;
  targetName: string;
}) {
  const { summary, worker } = detail;
  const points = series?.points ?? [];
  const lastPoint = points[points.length - 1];
  const sourceCount =
    worker?.committed_by_target[summary.data_source] ??
    summary.writes.committed_source;
  const targetCount =
    worker?.committed_by_target[summary.data_target] ??
    summary.writes.committed_target;
  const rows = [
    {
      key: "source",
      name: sourceName,
      role: "원본",
      dataName: summary.data_source,
      count: sourceCount,
      rate: lastPoint?.write_rate_source ?? null,
      ...firstLastWrite(points, "committed_source"),
    },
    {
      key: "target",
      name: targetName,
      role: "대상",
      dataName: summary.data_target,
      count: targetCount,
      rate: lastPoint?.write_rate_target ?? null,
      ...firstLastWrite(points, "committed_target"),
    },
  ];
  const max = Math.max(1, ...rows.map((r) => r.count));
  return (
    <div className="table-wrap">
      <table className="pk-table">
        <thead>
          <tr>
            <th>환경</th>
            <th>역할</th>
            <th>첫 쓰기</th>
            <th>마지막 쓰기</th>
            <th>현재 쓰기 대상</th>
            <th className="num">누적 쓰기</th>
            <th className="num">초당 쓰기</th>
            <th className="num">응답 p95</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const active = worker?.target === row.dataName;
            return (
              <tr key={row.key}>
                <td className="strong">
                  {row.name} <span className="sub">{row.dataName}</span>
                </td>
                <td>{row.role}</td>
                <td>
                  <WriteTime at={row.first} />
                </td>
                <td>
                  <WriteTime at={row.last} />
                </td>
                <td>
                  {active ? (
                    <StatusDot tone="ok">쓰는 중</StatusDot>
                  ) : (
                    <span className="sub">-</span>
                  )}
                </td>
                <td className="num">
                  <BarCell value={row.count} max={max}>
                    {formatCount(row.count)}
                  </BarCell>
                </td>
                <td className="num">
                  {row.rate === null ? "-" : formatRate(row.rate)}
                </td>
                <td className="num">
                  {active ? formatSeconds(worker?.latency.p95 ?? null) : "-"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
