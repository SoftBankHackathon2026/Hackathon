import { useQuery } from "@tanstack/react-query";
import { StatusDot } from "../../components/StatusDot";
import { taskPlanQueryOptions } from "./queries";
import { STEP_STATUS_LABELS, STEP_STATUS_TONES, stepNote } from "./taskPlan";

export function TaskPlanRoute({ jobId }: { jobId: string }) {
  const plan = useQuery(taskPlanQueryOptions(jobId));
  return plan.data ? (
    <span className="tag">{plan.data.route.label}</span>
  ) : null;
}

export function TaskPlanPanel({ jobId }: { jobId: string }) {
  const plan = useQuery(taskPlanQueryOptions(jobId));
  if (plan.isError)
    return <p className="empty">작업 단계를 불러오지 못했어요.</p>;
  if (!plan.data)
    return <p className="empty">작업 단계를 불러오는 중이에요.</p>;
  let order = 0;
  return (
    <div className="plan-phases">
      {plan.data.phases.map((phase, index) => (
        <section key={phase.id} className="plan-phase" aria-label={phase.title}>
          <header className="plan-phase-head">
            <h3>
              {index + 1}. {phase.title}
            </h3>
            <StatusDot tone={STEP_STATUS_TONES[phase.status]}>
              {STEP_STATUS_LABELS[phase.status]}
            </StatusDot>
          </header>
          <ol className="plan-steps">
            {phase.steps.map((step) => {
              order += 1;
              const note = stepNote(step);
              return (
                <li
                  key={step.id}
                  className={`plan-step${step.status === "unsupported" ? " muted" : ""}`}
                >
                  <span className="plan-step-index">{order}</span>
                  <div className="plan-step-body">
                    <div className="plan-step-title">
                      <strong>{step.title}</strong>
                      <StatusDot tone={STEP_STATUS_TONES[step.status]}>
                        {STEP_STATUS_LABELS[step.status]}
                      </StatusDot>
                    </div>
                    <p>{step.description}</p>
                    {step.tool && (
                      <code className="plan-step-tool">{step.tool}</code>
                    )}
                    {note && <p className="plan-step-note">{note}</p>}
                  </div>
                </li>
              );
            })}
          </ol>
        </section>
      ))}
    </div>
  );
}
