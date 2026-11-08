import { useEffect, useRef, useState } from "react";
import type { Fleet, MigrationSummary } from "../../api/migrations";
import { migrationHash } from "../../app/routes";
import { StatusLabel } from "../../components/PageKit";
import { ProviderLogo } from "../../components/ProviderLogo";
import { useSort } from "../../components/SortableTable";
import {
  formatAgo,
  formatCount,
  formatRatio,
  formatSeconds,
} from "../../lib/format";
import {
  chosenIds,
  headerCheck,
  LOCKED_TITLE,
  rowCheck,
  showsChecks,
  toggleAll,
  toggleTask,
} from "./deletion";
import { phaseIndex } from "./flow";
import { DIRECTION_LABELS, envShortName } from "./fleetView";
import { PhaseChevrons } from "./PhaseChevrons";
import { statusTone, STATUS_LABELS } from "./stages";
import { failedWrites } from "./summary";
import { TaskDeleteBar, type OnDeleted } from "./TaskDelete";

/**
 * 작업 표: 열을 눌러 정렬, 행(또는 이름)을 누르면 작업 상세 화면.
 * 등록한 작업은 앞 칸에서 골라 지울 수 있다. 설정 파일 작업은 칸이 없고, 진행 중인 작업은 칸이 잠긴다.
 */
export function JobsTable({
  fleet,
  rows,
  onOpen,
  notice = null,
  onPick = () => {},
  onDeleted = () => {},
}: {
  fleet: Fleet;
  rows: MigrationSummary[];
  onOpen: (jobId: string) => void;
  notice?: string | null;
  onPick?: () => void;
  onDeleted?: OnDeleted;
}) {
  const [picked, setPickedState] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const setPicked = (next: ReadonlySet<string>) => {
    setPickedState(next);
    onPick();
  };
  const checks = showsChecks(fleet, rows);
  const check = headerCheck(picked, rows);
  const headRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (headRef.current) headRef.current.indeterminate = check === "some";
  }, [check, checks]);
  const { sorted, th } = useSort(rows, {
    job: (m) => m.job_id,
    status: (m) => STATUS_LABELS[m.status],
    stage: (m) =>
      m.status === "pending" ? -1 : phaseIndex(m) * 100 + m.stage_index,
    source: (m) => m.writes.committed_source,
    ratio: (m) =>
      m.writes.committed_source > 0
        ? m.writes.committed_target / m.writes.committed_source
        : null,
    failed: failedWrites,
    pause: (m) => m.downtime.write_pause_seconds,
    updated: (m) => (m.updated_at ? Date.parse(m.updated_at) : null),
  });
  const bar = (checks || notice) && (
    <TaskDeleteBar
      selectable={checks}
      jobIds={checks ? chosenIds(picked, rows) : []}
      notice={notice}
      onDeleted={(ids, text) => {
        setPickedState(new Set());
        onDeleted(ids, text);
      }}
    />
  );
  if (rows.length === 0)
    return (
      <>
        {bar}
        <p className="pk-empty">
          조건에 맞는 작업이 없어요. 필터를 바꿔 보세요.
        </p>
      </>
    );
  const provider = (envId: string) =>
    fleet.environments.find((e) => e.id === envId)?.provider;
  return (
    <>
      {bar}
      <div
        className="table-wrap"
        tabIndex={0}
        role="region"
        aria-label="이전 작업 목록"
      >
        <table className="pk-table sticky">
          <thead>
            <tr>
              {checks && (
                <th className="task-check-cell">
                  <input
                    ref={headRef}
                    type="checkbox"
                    aria-label="보이는 작업 모두 선택"
                    checked={check === "all"}
                    disabled={!rows.some((m) => m.can_delete)}
                    onChange={() => setPicked(toggleAll(picked, rows))}
                  />
                </th>
              )}
              {th("job", "작업")}
              {th("status", "상태")}
              <th>경로</th>
              {th("stage", "단계")}
              {th("source", "원본 쓰기", "num")}
              {th("ratio", "대상 반영", "num")}
              {th("failed", "실패", "num")}
              {th("pause", "쓰기 중단", "num")}
              {th("updated", "갱신", "num")}
            </tr>
          </thead>
          <tbody>
            {sorted.map((m) => {
              const failed = failedWrites(m);
              return (
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
                  {checks && (
                    <td
                      className="task-check-cell"
                      onClick={(event) => event.stopPropagation()}
                      onKeyDown={(event) => event.stopPropagation()}
                    >
                      <CheckCell
                        job={m}
                        picked={picked.has(m.job_id)}
                        onToggle={() => setPicked(toggleTask(picked, m.job_id))}
                      />
                    </td>
                  )}
                  <td>
                    <a
                      className="strong-link"
                      href={migrationHash(m.job_id)}
                      onClick={(event) => event.stopPropagation()}
                    >
                      {m.job_id}
                    </a>{" "}
                    <span className="sub">{DIRECTION_LABELS[m.direction]}</span>
                  </td>
                  <td>
                    <StatusLabel tone={statusTone(m.status)}>
                      {STATUS_LABELS[m.status]}
                    </StatusLabel>
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
                    <PhaseChevrons job={m} />
                  </td>
                  <td className="num">
                    {formatCount(m.writes.committed_source)}
                  </td>
                  <td className="num">
                    <RatioCell
                      part={m.writes.committed_target}
                      whole={m.writes.committed_source}
                    />
                  </td>
                  <td className={`num${failed > 0 ? " text-bad" : ""}`}>
                    {formatCount(failed)}
                  </td>
                  <td className="num">
                    {formatSeconds(m.downtime.write_pause_seconds)}
                  </td>
                  <td className="num sub">{formatAgo(m.updated_at)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

function CheckCell({
  job,
  picked,
  onToggle,
}: {
  job: MigrationSummary;
  picked: boolean;
  onToggle: () => void;
}) {
  const state = rowCheck(job);
  if (state === "none") return null;
  return (
    <label
      className="task-check"
      title={state === "locked" ? LOCKED_TITLE : undefined}
    >
      <input
        type="checkbox"
        aria-label={`${job.job_id} 선택`}
        checked={state === "open" && picked}
        disabled={state === "locked"}
        onChange={onToggle}
      />
    </label>
  );
}

/** 원본 대비 대상에 반영된 비율. 두 절대값을 나란히 두는 대신 비교를 표에 넣는다. */
function RatioCell({ part, whole }: { part: number; whole: number }) {
  if (whole <= 0) return <span className="sub">-</span>;
  const ratio = Math.min(1, part / whole);
  return (
    <span
      className="ratio-cell"
      title={`${formatCount(part)} / ${formatCount(whole)}`}
    >
      <span className="ratio-track" aria-hidden="true">
        <span style={{ width: `${ratio * 100}%` }} />
      </span>
      {formatRatio(part, whole)}
    </span>
  );
}
