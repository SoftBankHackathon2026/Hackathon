import type { ReactNode } from "react";
import type { Severity } from "../api/security";
import { severityLevel } from "../features/security/severityBars";
import { SEVERITY_LABELS } from "../features/security/posture";

export function SeverityDot({
  severity,
  children,
}: {
  severity: Severity;
  children: ReactNode;
}) {
  return (
    <span className="status">
      <i className={`dot sev-dot-${severity}`} aria-hidden="true" />
      {children}
    </span>
  );
}

export function SeverityBars({ severity }: { severity: Severity }) {
  const level = severityLevel(severity);
  return (
    <span
      className={`sev-bars${severity === "critical" ? " critical" : ""}`}
      role="img"
      aria-label={SEVERITY_LABELS[severity]}
    >
      {[1, 2, 3].map((n) => (
        <i key={n} className={n <= level ? "on" : undefined} />
      ))}
    </span>
  );
}
