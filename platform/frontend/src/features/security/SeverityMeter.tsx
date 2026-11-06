import type { Severity } from "../../api/security";
import { SEVERITY_LABELS } from "./posture";

const LEVEL: Record<Severity, number> = {
  critical: 4,
  high: 3,
  medium: 2,
  low: 1,
};

/** 심각도 막대 표식(Wiz·Defender 방식): 네 칸 중 심각도만큼 채움 + 글자. */
export function SeverityMeter({
  severity,
  label = true,
}: {
  severity: Severity;
  label?: boolean;
}) {
  return (
    <span
      className={`sev-meter sev-${severity}`}
      title={SEVERITY_LABELS[severity]}
    >
      <span className="sev-meter-bars" aria-hidden="true">
        {[1, 2, 3, 4].map((i) => (
          <i key={i} className={i <= LEVEL[severity] ? "on" : undefined} />
        ))}
      </span>
      {label ? (
        SEVERITY_LABELS[severity]
      ) : (
        <span className="sr-only">{SEVERITY_LABELS[severity]}</span>
      )}
    </span>
  );
}
