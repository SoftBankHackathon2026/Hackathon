import type { VerifyReport } from "../../api/migrations.ts";

export function verifyTotal(report: VerifyReport | null) {
  if (!report) return null;
  const c = report.counts;
  return c.B_minus_T + c.T_minus_B + c.field_mismatch + c.S_minus_T;
}

export function verifyRows(report: VerifyReport | null) {
  return [
    { key: "B_minus_T", label: "B−T", help: "원본에만 있는 행" },
    { key: "T_minus_B", label: "T−B", help: "대상에만 있는 행" },
    {
      key: "field_mismatch",
      label: "필드 불일치",
      help: "양쪽에 있으나 값이 다른 행",
    },
    {
      key: "S_minus_T",
      label: "S−T",
      help: "커밋 기록은 있으나 대상에 없는 요청",
    },
  ].map((row) => ({
    ...row,
    value: report
      ? report.counts[row.key as keyof VerifyReport["counts"]]
      : null,
  }));
}
