import type { Severity } from "../../api/security.ts";

export function severityLevel(severity: Severity) {
  switch (severity) {
    case "critical":
    case "high":
      return 3;
    case "medium":
      return 2;
    default:
      return 1;
  }
}
