import type { FleetEnvironment } from "../../api/migrations";
import { StatusDot } from "../../components/StatusDot";
import { appCell } from "./argo";
import { appRows } from "./fleetView";

export function AppMatrix({
  environments,
}: {
  environments: FleetEnvironment[];
}) {
  // 문제 있는 앱을 위로: 가장 나쁜 상태 순(문제 → 다름 → 배포 중 → 정상 → 미수집).
  const rank = { bad: 0, warn: 1, running: 2, ok: 3, idle: 4 } as const;
  const worst = (name: string) =>
    Math.min(
      ...environments.map((env) => {
        const app = env.apps.find((a) => a.name === name);
        return app ? rank[appCell(app).tone] : 9;
      }),
    );
  const names = appRows(environments).sort((a, b) => worst(a) - worst(b));
  if (names.length === 0)
    return <p className="empty-line">수집된 앱이 없어요.</p>;
  return (
    <div className="table-wrap">
      <table className="pk-table">
        <thead>
          <tr>
            <th>앱</th>
            {environments.map((env) => (
              <th key={env.id}>{env.name}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {names.map((name) => (
            <tr key={name}>
              <td className="strong">{name}</td>
              {environments.map((env) => {
                const app = env.apps.find((a) => a.name === name);
                const cell = appCell(app);
                return (
                  <td key={env.id}>
                    {app ? (
                      <StatusDot tone={cell.tone}>{cell.label}</StatusDot>
                    ) : (
                      <span className="sub">-</span>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
