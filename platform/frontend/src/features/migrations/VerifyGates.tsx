import type { Gate, VerifyReport } from "../../api/migrations";
import { StateLabel } from "../../components/StatusDot";
import { formatCount, formatDateTime } from "../../lib/format";
import { GATE_STAGES, outcomeTone, OUTCOME_LABELS, reasonLabel } from "./gates";
import { verifyRows } from "./proof";

function count(value: number | null) {
  if (value === null) return <span className="sub">-</span>;
  return (
    <span className={value > 0 ? "text-bad" : undefined}>
      {formatCount(value)}
    </span>
  );
}

export function VerifyTable({
  pre,
  post,
}: {
  pre: VerifyReport | null;
  post: VerifyReport | null;
}) {
  const before = verifyRows(pre);
  const after = verifyRows(post);
  const totals: { label: string; pick: (r: VerifyReport) => number }[] = [
    { label: "원본 건수", pick: (r) => r.source_count },
    { label: "대상 건수", pick: (r) => r.target_count },
    { label: "반영된 쓰기 건수", pick: (r) => r.committed_count },
  ];
  return (
    <div className="table-wrap">
      <table className="dt compact">
        <thead>
          <tr>
            <th>항목</th>
            <th className="num">전환 전</th>
            <th className="num">전환 후</th>
          </tr>
        </thead>
        <tbody>
          {before.map((row, i) => (
            <tr key={row.key}>
              <td title={row.help}>
                <span className="strong">{row.label}</span>{" "}
                <span className="sub">{row.help}</span>
              </td>
              <td className="num">{count(row.value)}</td>
              <td className="num">{count(after[i].value)}</td>
            </tr>
          ))}
          {totals.map((row) => (
            <tr key={row.label} className="muted-row">
              <td>{row.label}</td>
              <td className="num">
                {pre ? (
                  formatCount(row.pick(pre))
                ) : (
                  <span className="sub">-</span>
                )}
              </td>
              <td className="num">
                {post ? (
                  formatCount(row.pick(post))
                ) : (
                  <span className="sub">-</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function GatesTable({ gates }: { gates: Gate[] }) {
  const rows = [...gates].sort(
    (a, b) => Date.parse(b.evaluated_at) - Date.parse(a.evaluated_at),
  );
  return (
    <div className="table-wrap">
      <table className="dt compact">
        <thead>
          <tr>
            <th>시각</th>
            <th>게이트</th>
            <th>결과</th>
            <th>사유 코드</th>
            <th>정책 버전</th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={5} className="sub">
                아직 평가 기록이 없어요.
              </td>
            </tr>
          )}
          {rows.map((gate) => (
            <tr key={gate.decision_id}>
              <td className="sub">{formatDateTime(gate.evaluated_at)}</td>
              <td>
                {GATE_STAGES.find((s) => s.id === gate.stage)?.title ??
                  gate.stage}
              </td>
              <td>
                <StateLabel tone={outcomeTone(gate.outcome)}>
                  {OUTCOME_LABELS[gate.outcome]}
                </StateLabel>
              </td>
              <td>
                {gate.reason_codes.length === 0 ? (
                  <span className="sub">-</span>
                ) : (
                  gate.reason_codes.map((code) => (
                    <span
                      className="mono code"
                      key={code}
                      title={reasonLabel(code)}
                    >
                      {code}
                    </span>
                  ))
                )}
              </td>
              <td className="sub">{gate.policy_version ?? "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
