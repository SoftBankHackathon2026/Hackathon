import type { Control, EnvironmentScore, Severity } from "../../api/security";
import {
  SEVERITY_LABELS,
  SEVERITY_ORDER,
  hasEnoughEvidence,
  scoreBand,
  standardCompliance,
  type StandardEnvStats,
} from "./posture";

function Cell({ stats }: { stats: StandardEnvStats }) {
  if (stats.total === 0) return <span className="sub">-</span>;
  const enough = hasEnoughEvidence(stats.pass + stats.fail, stats.total);
  const failing = SEVERITY_ORDER.filter(
    (s: Severity) => stats.failedBySeverity[s] > 0,
  );
  const share = Math.round((stats.pass / stats.total) * 100);
  return (
    <div className="pk-std-cell">
      <div className="pk-std-top">
        {enough && stats.score !== null ? (
          <span className={`pk-score band-${scoreBand(stats.score)}`}>
            {stats.score}
            <small>점</small>
          </span>
        ) : (
          <span className="sub">근거 부족</span>
        )}
        <span className="sub">
          {stats.total}개 중 {stats.pass}개 통과
        </span>
      </div>
      <div className="pk-track" aria-hidden="true">
        <span style={{ width: `${share}%` }} />
      </div>
      {failing.length > 0 && (
        <div className="pk-std-fails">
          {failing.map((s) => (
            <span key={s} className={`pk-label sev-${s}`}>
              {SEVERITY_LABELS[s]} {stats.failedBySeverity[s]}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export function StandardsTable({
  standards,
  controls,
  environments,
  onSelect,
}: {
  standards: { id: string; name: string }[];
  controls: Control[];
  environments: EnvironmentScore[];
  onSelect: (standardId: string) => void;
}) {
  const rows = standardCompliance(
    standards,
    controls,
    environments.map((e) => e.env_id),
  ).filter((row) => row.members > 0);
  if (rows.length === 0)
    return <p className="pk-empty">연결된 보안 표준이 없어요.</p>;
  return (
    <div className="table-wrap">
      <table className="pk-table pk-std">
        <thead>
          <tr>
            <th>표준</th>
            {environments.map((env) => (
              <th key={env.env_id}>{env.name}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.standard.id}
              className="clickable"
              onClick={() => onSelect(row.standard.id)}
            >
              <td>
                <button
                  type="button"
                  className="pk-std-name"
                  onClick={(event) => {
                    event.stopPropagation();
                    onSelect(row.standard.id);
                  }}
                >
                  {row.standard.name}
                </button>
                <div className="sub">
                  {row.standard.id} · 통제 {row.members}개
                </div>
              </td>
              {environments.map((env) => (
                <td key={env.env_id}>
                  <Cell stats={row.envs[env.env_id]} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
