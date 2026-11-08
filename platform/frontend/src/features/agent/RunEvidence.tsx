import { StatusDot } from "../../components/StatusDot";
import { formatSeconds } from "../../lib/format";
import {
  type FixView,
  type Hypothesis,
  HYPOTHESIS_LABELS,
  hypothesisTone,
  SANDBOX_STEP_LABELS,
  type SandboxView,
} from "./evidence";

const DIFF_SIGNS = { same: " ", del: "-", add: "+" } as const;

export function HypothesisTree({
  items,
  trigger,
  codes,
}: {
  items: Hypothesis[];
  trigger: string;
  codes: string[];
}) {
  const found = items.filter((item) => item.status === "validated").length;
  return (
    <div className="agent-hyp">
      <div className="agent-hyp-root">
        <span className="agent-hyp-title">{trigger}</span>
        {codes.length > 0 && (
          <span className="agent-hyp-sub mono">{codes.join(", ")}</span>
        )}
        <span className="agent-hyp-sub">
          가설 {items.length}개 중 {found}개를 원인으로 확인했어요.
        </span>
      </div>
      <ul className="agent-hyp-branches">
        {items.map((item) => (
          <li
            key={item.id}
            className={`agent-hyp-node is-${item.status}`}
            aria-label={`${item.title}: ${HYPOTHESIS_LABELS[item.status]}`}
          >
            <div className="agent-hyp-head">
              <StatusDot tone={hypothesisTone(item.status)}>
                {HYPOTHESIS_LABELS[item.status]}
              </StatusDot>
              <span className="agent-hyp-title">{item.title}</span>
              {item.key && (
                <span className="agent-hyp-key mono">{item.key}</span>
              )}
            </div>
            {item.evidence && <p className="agent-hyp-sub">{item.evidence}</p>}
            {item.status === "validated" && (
              <p className="agent-hyp-next">
                {item.fixed
                  ? `AI가 ${item.key} 값을 고쳤어요.`
                  : "AI가 고칠 수 있는 값이 아니라 사람이 확인해야 해요."}
              </p>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function FixDiff({ fix }: { fix: FixView }) {
  return (
    <div className="agent-fix">
      {fix.cause && <p className="agent-text">{fix.cause}</p>}
      {fix.diff ? (
        <figure className="agent-diff">
          <figcaption className="mono">{fix.path}</figcaption>
          <pre>
            {fix.diff.map((line, index) => (
              <code key={index} className={`agent-diff-line is-${line.kind}`}>
                <span className="agent-diff-sign" aria-hidden="true">
                  {DIFF_SIGNS[line.kind]}
                </span>
                {line.kind !== "same" && (
                  <span className="sr-only">
                    {line.kind === "del" ? "지운 줄 " : "더한 줄 "}
                  </span>
                )}
                {line.text}
              </code>
            ))}
          </pre>
        </figure>
      ) : (
        <ul className="agent-changes">
          {fix.changes.map((change) => (
            <li key={change.key} className="mono">
              {change.key}: {change.before ?? "-"} → {change.after}
            </li>
          ))}
        </ul>
      )}
      {(fix.impact || fix.checked) && (
        <dl className="agent-kv">
          {fix.impact && (
            <>
              <dt>영향</dt>
              <dd>{fix.impact}</dd>
            </>
          )}
          {fix.checked && (
            <>
              <dt>AI 확인</dt>
              <dd>{fix.checked}</dd>
            </>
          )}
        </dl>
      )}
    </div>
  );
}

export function SandboxSteps({ box }: { box: SandboxView }) {
  const meta = [box.runner, box.model, formatSeconds(box.seconds)].filter(
    (value) => value && value !== "-",
  );
  return (
    <div className="agent-sbx">
      <p className="agent-sbx-meta">
        {meta.join(", ")}
        {box.image && (
          <>
            {meta.length > 0 && ", "}
            <span className="mono">{box.image}</span>
          </>
        )}
      </p>
      {box.steps.length === 0 ? (
        <p className="agent-text">샌드박스 작업 기록이 없어요.</p>
      ) : (
        <ol className="agent-sbx-steps">
          {box.steps.map((step, index) => (
            <li key={index} className={`is-${step.kind}`}>
              <span className="agent-sbx-kind">
                {SANDBOX_STEP_LABELS[step.kind]}
              </span>
              <div className="agent-sbx-body">
                {step.kind === "run" || step.kind === "edit" ? (
                  <code className="mono">{step.text}</code>
                ) : (
                  <p>{step.text}</p>
                )}
                {step.ok === false && <StatusDot tone="bad">실패</StatusDot>}
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
