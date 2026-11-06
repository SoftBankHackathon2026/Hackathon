import type { MigrationSummary } from "../../api/migrations";
import { phaseIndex, PHASES } from "./flow";
import { STATUS_LABELS, statusTone } from "./stages";

/**
 * 작업 진행 화살표(AWS MGN 수명 주기 방식): 6묶음을 이어 붙인 화살표, 지난 칸은 채우고 지금 칸은 상태색.
 * 아래에 지금 묶음 이름과 상태.
 */
export function PhaseChevrons({
  job,
}: {
  job: Pick<MigrationSummary, "stage" | "status">;
}) {
  const at = phaseIndex(job);
  const started = job.status !== "pending";
  const done = job.status === "completed";
  const tone = statusTone(job.status);
  return (
    <span className="chev" title={PHASES.map((p) => p.title).join(" → ")}>
      <span className="chev-row" aria-hidden="true">
        {PHASES.map((phase, i) => {
          const state =
            done || i < at
              ? "done"
              : i === at
                ? `now tone-${started ? tone : "idle"}`
                : "";
          return <i key={phase.id} className={state} />;
        })}
      </span>
      <span className="chev-text">
        <b>{done ? "완료" : PHASES[at].title}</b>
        <span className="sub">{STATUS_LABELS[job.status]}</span>
      </span>
    </span>
  );
}
