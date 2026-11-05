import type { Fleet, MigrationSummary } from "../../api/migrations";
import { BarCell } from "../../components/BarCell";
import { StatusLabel } from "../../components/PageKit";
import { ProviderLogo } from "../../components/ProviderLogo";
import { formatAgo, formatCount, formatSeconds } from "../../lib/format";
import { DIRECTION_LABELS, envShortName } from "./fleetView";
import { stageTitle, statusTone, STATUS_LABELS, stepLabel } from "./stages";

export function JobsTable({
  fleet,
  rows,
  onOpen,
}: {
  fleet: Fleet;
  rows: MigrationSummary[];
  onOpen: (jobId: string) => void;
}) {
  if (rows.length === 0)
    return <p className="pk-empty">조건에 맞는 작업이 없어요.</p>;
  const max = (pick: (m: MigrationSummary) => number) =>
    Math.max(0, ...rows.map(pick));
  const maxSource = max((m) => m.writes.committed_source);
  const maxTarget = max((m) => m.writes.committed_target);
  const maxPause = max((m) => m.downtime.write_pause_seconds ?? 0);
  const provider = (envId: string) =>
    fleet.environments.find((e) => e.id === envId)?.provider;
  return (
    <>
      <p className="pk-count">
        {rows.length}개 중 1–{rows.length}
      </p>
      <div className="table-wrap">
        <table className="pk-table">
          <thead>
            <tr>
              <th className="pk-status-col">상태</th>
              <th>작업</th>
              <th>경로</th>
              <th>단계</th>
              <th className="num">원본 DB 쓰기</th>
              <th className="num">대상 DB 쓰기</th>
              <th className="num">쓰기 중단</th>
              <th className="num">마지막 갱신</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((m) => (
              <tr
                key={m.job_id}
                className="clickable"
                tabIndex={0}
                onClick={() => onOpen(m.job_id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onOpen(m.job_id);
                  }
                }}
              >
                <td className="pk-status-col">
                  <StatusLabel tone={statusTone(m.status)}>
                    {STATUS_LABELS[m.status]}
                  </StatusLabel>
                </td>
                <td>
                  <a
                    className="strong-link"
                    href={`#migrations/${encodeURIComponent(m.job_id)}`}
                    onClick={(event) => event.stopPropagation()}
                  >
                    {m.job_id}
                  </a>{" "}
                  <span className="sub">{DIRECTION_LABELS[m.direction]}</span>
                </td>
                <td>
                  <span className="pk-path">
                    <ProviderLogo provider={provider(m.source_env)} />
                    {envShortName(fleet.environments, m.source_env)}
                    <span className="sub">→</span>
                    <ProviderLogo provider={provider(m.target_env)} />
                    {envShortName(fleet.environments, m.target_env)}
                  </span>
                </td>
                <td>
                  {stageTitle(m.stage)}{" "}
                  <span className="sub">{stepLabel(m)}단계</span>
                </td>
                <td className="num">
                  <BarCell value={m.writes.committed_source} max={maxSource}>
                    {formatCount(m.writes.committed_source)}
                  </BarCell>
                </td>
                <td className="num">
                  <BarCell value={m.writes.committed_target} max={maxTarget}>
                    {formatCount(m.writes.committed_target)}
                  </BarCell>
                </td>
                <td className="num">
                  <BarCell
                    value={m.downtime.write_pause_seconds ?? 0}
                    max={maxPause}
                  >
                    {formatSeconds(m.downtime.write_pause_seconds)}
                  </BarCell>
                </td>
                <td className="num sub">{formatAgo(m.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
