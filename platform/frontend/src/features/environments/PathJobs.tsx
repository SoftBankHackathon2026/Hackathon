import type { FleetEnvironment, MigrationSummary } from "../../api/migrations";
import { StatusDot } from "../../components/StatusDot";
import { STATUS_LABELS, stageTitle, statusTone } from "../migrations/stages";
import { isActive, orderedJobs } from "./pathJobOrder";

export function PathJobs({
  migrations,
  environments,
  selected,
  onSelect,
}: {
  migrations: MigrationSummary[];
  environments: FleetEnvironment[];
  selected: string | null;
  onSelect: (jobId: string) => void;
}) {
  const jobs = orderedJobs(migrations);
  const active = jobs.filter((job) => isActive(job.status)).length;
  const name = (id: string) =>
    environments.find((env) => env.id === id)?.name ?? id;
  return (
    <div className="path-jobs">
      <div className="path-jobs-head">
        <strong>이전 작업 {jobs.length}개</strong>
        {active > 0 && <span>진행 중 {active}개</span>}
        {selected && (
          <a href={"#migrations/" + encodeURIComponent(selected)}>
            작업 화면 열기
          </a>
        )}
      </div>
      <div className="path-jobs-list" role="listbox" aria-label="이전 작업">
        {jobs.map((job) => (
          <button
            key={job.job_id}
            type="button"
            role="option"
            aria-selected={job.job_id === selected}
            className={`path-job${isActive(job.status) ? " active" : ""}`}
            onClick={() => onSelect(job.job_id)}
          >
            <span className="path-job-top">
              <strong>{job.job_id}</strong>
              <StatusDot tone={statusTone(job.status)}>
                {STATUS_LABELS[job.status]}
              </StatusDot>
            </span>
            <span className="path-job-route">
              {name(job.source_env)} → {name(job.target_env)}
            </span>
            <span className="path-job-stage">
              {stageTitle(job.stage)}
              {job.stage ? ` · ${job.stage_index + 1}/${job.stage_count}` : ""}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
