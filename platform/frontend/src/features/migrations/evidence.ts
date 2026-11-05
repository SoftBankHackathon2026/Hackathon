export function formatEvidence(value: unknown): string {
  if (value === null || value === undefined) return "-";
  if (typeof value === "boolean") return value ? "예" : "아니오";
  if (typeof value === "number") return value.toLocaleString("ko-KR");
  if (typeof value === "string") return value === "" ? "-" : value;
  if (Array.isArray(value)) return value.map(formatEvidence).join(", ");
  return JSON.stringify(value);
}
