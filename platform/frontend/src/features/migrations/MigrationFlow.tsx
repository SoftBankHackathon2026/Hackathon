import type { FleetEnvironment, MigrationSummary } from "../../api/migrations";
import { migrationHash } from "../../app/routes";
import { flowOverview, flowPhases, type PhaseId } from "./flow";
import { STATUS_LABELS, statusTone } from "./stages";

const GLYPHS: Record<PhaseId, string> = {
  prepare: "M9 7V3M15 7V3M6 7h12v4a6 6 0 0 1-12 0zM12 17v4",
  precheck: "M9 4h6v3H9zM7 5.5H5v15h14v-15h-2M9 13l2 2 4-4",
  copy: "M8 8h11v11H8zM5 16V5h11",
  approve:
    "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM2 20c1-3.5 4-5 7-5 1.4 0 2.7.3 3.8.9M15 18l2 2 4-4",
  switch: "M4 8h13M13 4l4 4-4 4M20 16H7M11 12l-4 4 4 4",
  verify: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM9 12l2 2 4-4",
};

const COL = 100;
const MID = 22;

/** 흐름 띠: 열마다 그 단계까지 온 작업 수만큼 두꺼운 띠(Wiz 노출 관리 개요 방식, 단색). */
function FlowBand({ reached, total }: { reached: number[]; total: number }) {
  const h = reached.map((r) => 4 + (total > 0 ? (r / total) * 32 : 0));
  const xs = reached.map((_, i) => i * COL + COL / 2);
  const w = reached.length * COL;
  const top = (i: number) => MID - h[i] / 2;
  const bottom = (i: number) => MID + h[i] / 2;
  let d = `M0 ${top(0)}L${xs[0]} ${top(0)}`;
  for (let i = 1; i < xs.length; i++) {
    const cx = (xs[i - 1] + xs[i]) / 2;
    d += `C${cx} ${top(i - 1)} ${cx} ${top(i)} ${xs[i]} ${top(i)}`;
  }
  d += `L${w} ${top(xs.length - 1)}L${w} ${bottom(xs.length - 1)}L${xs[xs.length - 1]} ${bottom(xs.length - 1)}`;
  for (let i = xs.length - 2; i >= 0; i--) {
    const cx = (xs[i + 1] + xs[i]) / 2;
    d += `C${cx} ${bottom(i + 1)} ${cx} ${bottom(i)} ${xs[i]} ${bottom(i)}`;
  }
  d += `L0 ${bottom(0)}Z`;
  return (
    <svg
      className="fl-band-svg"
      viewBox={`0 0 ${w} ${MID * 2}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <path d={d} />
    </svg>
  );
}

/**
 * 이전 흐름: 마이그레이션이 거치는 6단계를 한 패널의 열로.
 * 열 = 단계 이름 · 지금 있는 작업 수 · (흐름 띠) · 하는 일 · 단계 지표 · 작업.
 */
export function MigrationFlow({
  migrations,
  environments,
}: {
  migrations: MigrationSummary[];
  environments: FleetEnvironment[];
}) {
  const cols = flowOverview(migrations, environments);
  const { exceptions } = flowPhases(migrations);
  const cls = (c: (typeof cols)[number]) =>
    `fl-cell${c.index === 0 ? " first" : ""}${c.jobs.length > 0 ? " has-jobs" : ""}${
      c.phase.approval && c.jobs.some((m) => m.status === "waiting_approval")
        ? " needs-human"
        : ""
    }`;
  return (
    <>
      <div className="fl" role="list" aria-label="이전 흐름">
        {cols.map((c) => (
          <div
            key={`h-${c.phase.id}`}
            className={`${cls(c)} fl-head`}
            role="listitem"
          >
            <span className="fl-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24">
                <path d={GLYPHS[c.phase.id]} />
              </svg>
            </span>
            <span className="fl-title">
              <span className="fl-no">{c.index + 1}</span>
              {c.phase.title}
            </span>
            {c.phase.pause && <span className="fl-flag">쓰기 일시 중단</span>}
            {c.phase.approval && <span className="fl-flag">수동 승인</span>}
          </div>
        ))}
        {cols.map((c) => (
          <div key={`n-${c.phase.id}`} className={`${cls(c)} fl-num`}>
            <b>{c.jobs.length}</b>
            <small>작업</small>
          </div>
        ))}
        <div className="fl-band">
          <FlowBand
            reached={cols.map((c) => c.reached)}
            total={cols[0]?.total ?? 0}
          />
        </div>
        {cols.map((c) => (
          <div key={`d-${c.phase.id}`} className={`${cls(c)} fl-body`}>
            <p className="fl-desc">{c.phase.description}</p>
            <dl className="fl-facts">
              {c.facts.map((f) => (
                <div key={f.label}>
                  <dt>{f.label}</dt>
                  <dd className={f.tone ? `tone-${f.tone}` : undefined}>
                    {f.value}
                  </dd>
                </div>
              ))}
            </dl>
            {c.phase.id === "prepare" && (
              <a className="fl-link" href="#prepare">
                환경 연결하기
              </a>
            )}
            {c.jobs.length > 0 && (
              <ul className="fl-jobs">
                {c.jobs.map((job) => (
                  <li key={job.job_id}>
                    <a
                      href={migrationHash(job.job_id)}
                      className={`flow-job tone-${statusTone(job.status)}`}
                    >
                      <i aria-hidden="true" />
                      {job.job_id}
                      <span>{STATUS_LABELS[job.status]}</span>
                    </a>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
      {exceptions.length > 0 && (
        <p className="pk-alert">
          중단된 작업 {exceptions.length}개:{" "}
          {exceptions.map((job, i) => (
            <span key={job.job_id}>
              {i > 0 && ", "}
              <a href={migrationHash(job.job_id)}>
                <strong>{job.job_id}</strong>
              </a>{" "}
              {STATUS_LABELS[job.status]}
            </span>
          ))}
        </p>
      )}
    </>
  );
}
